from __future__ import annotations

import copy
import json as _json
import re
import traceback

from core.log_setup import get_logger as _get_logger
from core.normalize import brand_from_url, force_https, normalize_socials, twitter_to_x
from core.parser.contact import extract_contacts_from_github, extract_contacts_from_site
from core.parser.link_aggregator import (
    extract_socials_from_aggregator,
    is_link_aggregator,
    verify_aggregator_belongs,
)
from core.parser.twitter import (
    download_twitter_avatar,
    get_links_from_x_profile,
    reset_verified_state,
    select_verified_twitter,
)
from core.parser.web import (
    extract_project_name,
    extract_social_links,
    fetch_url_html,
    get_domain_name,
)
from core.parser.youtube import (
    youtube_oembed_title,
    youtube_to_handle,
    youtube_watch_to_embed,
)
from core.paths import PROJECT_ROOT

logger = _get_logger("collector")


# Основная точка: сбор main.json-подобной структуры по сайту
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
    from core.parser.link_aggregator import (
        extract_contacts_from_aggregator,
    )  # контакты из агрегатора
    from core.settings import (
        get_social_hosts,
    )  # для построения host→key мапа динамически

    reset_verified_state(full=False)
    _TMPL_PATH = PROJECT_ROOT / "core" / "templates" / "main_template.json"

    with open(_TMPL_PATH, "r", encoding="utf-8") as _f:
        _TEMPLATE_CANON = _json.load(_f)

    _CANON_KEYS = list((_TEMPLATE_CANON.get("socialLinks") or {}).keys())

    main_data = copy.deepcopy(main_template) if isinstance(main_template, dict) else {}

    # юнион-ключи: из переданного шаблона ∪ из канонического
    tmpl_keys = (
        list((main_template.get("socialLinks") or {}).keys())
        if isinstance(main_template, dict)
        else []
    )
    social_keys = list(dict.fromkeys([*tmpl_keys, *_CANON_KEYS]))

    # инициализация блока socialLinks и обязательный websiteURL
    website_url = force_https(website_url)
    main_data["socialLinks"] = {k: "" for k in social_keys}
    main_data["socialLinks"]["websiteURL"] = website_url

    # остальные поля каркаса
    main_data.setdefault("name", "")
    main_data.setdefault("contacts", {})
    main_data["contacts"].setdefault("emails", [])
    main_data["contacts"].setdefault("forms", [])
    main_data["contacts"].setdefault("persons", [])

    # helper: маппинг host -> socialKey из конфига (без жёсткого dict)
    def _build_host_key_map() -> dict:
        key_map: dict[str, str] = {}
        for h in get_social_hosts():
            if h in ("x.com", "twitter.com") or "twitter" in h or h.endswith(".x.com"):
                key_map[h] = "twitterURL"
            elif "discord" in h:
                key_map[h] = "discordURL"
            elif h == "t.me" or "telegram" in h:
                key_map[h] = "telegramURL"
            elif h == "youtu.be" or "youtube" in h:
                key_map[h] = "youtubeURL"
            elif h == "lnkd.in" or "linkedin" in h:
                key_map[h] = "linkedinURL"
            elif "reddit" in h:
                key_map[h] = "redditURL"
            elif "medium" in h:
                key_map[h] = "mediumURL"
            elif "github" in h:
                key_map[h] = "githubURL"
        return key_map

    _HOST_TO_KEY = _build_host_key_map()

    try:
        # грузим главную страницу проекта (auto: requests → при необходимости playwright)
        html = fetch_url_html(website_url, prefer="auto")

        # первичные соцсети/доки с главной
        socials = extract_social_links(html, website_url, is_main_page=True)
        socials = normalize_socials(socials)

        # перенос найденных соцсетей
        for k in social_keys:
            v = socials.get(k)
            if isinstance(v, str) and v.strip():
                main_data["socialLinks"][k] = v.strip()
        for k, v in (socials or {}).items():
            if isinstance(v, str) and v.strip():
                if k not in main_data["socialLinks"]:
                    main_data["socialLinks"][k] = ""
                main_data["socialLinks"][k] = v.strip()

        site_contacts = extract_contacts_from_site(html, website_url)
        if site_contacts.get("emails"):
            main_data["contacts"]["emails"] = list(
                dict.fromkeys([*main_data["contacts"]["emails"], *site_contacts["emails"]])
            )
        if site_contacts.get("forms"):
            main_data["contacts"]["forms"] = list(
                dict.fromkeys([*main_data["contacts"]["forms"], *site_contacts["forms"]])
            )

        # Контакты: GitHub
        gh = main_data["socialLinks"].get("githubURL") or ""
        if gh:
            gh_contacts = extract_contacts_from_github(gh)
            if gh_contacts.get("emails"):
                main_data["contacts"]["emails"] = list(
                    dict.fromkeys([*main_data["contacts"]["emails"], *gh_contacts["emails"]])
                )

        # разбор X/Twitter: выбор верифицированного, домерж из агрегатора, аватар
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""
        twitter_final = ""
        enriched_from_agg = {}
        aggregator_url = ""
        avatar_verified = ""

        try:
            # выбираем правильный twitter из найденных кандидатов
            res = select_verified_twitter(
                found_socials=main_data["socialLinks"],
                socials=socials,
                site_domain=site_domain,
                brand_token=brand_token,
                html=html,
                url=website_url,
                trust_home=False,
            )
            if isinstance(res, tuple):
                if len(res) == 4:
                    (
                        twitter_final,
                        enriched_from_agg,
                        aggregator_url,
                        avatar_verified,
                    ) = res
                elif len(res) == 3:
                    twitter_final, enriched_from_agg, aggregator_url = res
                elif len(res) >= 1:
                    twitter_final = res[0]

            if twitter_final:
                main_data["socialLinks"]["twitterURL"] = twitter_final

            # домерж соцсетей, полученных из агрегатора твиттера
            for k, v in (enriched_from_agg or {}).items():
                if k == "websiteURL" or not v:
                    continue
                if k in main_data["socialLinks"] and not main_data["socialLinks"][k]:
                    main_data["socialLinks"][k] = v

        except Exception as e:
            logger.warning("Twitter verification error: %s", e)

        try:
            # bio/аватар X + возможный линк-агрегатор в био
            bio = {}
            avatar_url = avatar_verified or ""
            need_bio_for_avatar = bool(
                main_data["socialLinks"].get("twitterURL") and (not avatar_url)
            )

            # подтянем display name из X (без запроса аватара)
            twitter_display = ""
            if main_data["socialLinks"].get("twitterURL"):
                try:
                    tw_profile = (
                        get_links_from_x_profile(
                            main_data["socialLinks"]["twitterURL"], need_avatar=False
                        )
                        or {}
                    )
                    twitter_display = (tw_profile.get("name") or "").strip()
                except Exception:
                    twitter_display = ""

            # если нужен аватар - повторим профиль с аватаром
            if need_bio_for_avatar:
                try:
                    bio = (
                        get_links_from_x_profile(
                            main_data["socialLinks"]["twitterURL"], need_avatar=True
                        )
                        or {}
                    )
                except Exception:
                    bio = {}

            # cобираем из bio все ссылки + помечаем агрегатор
            aggregator_from_bio = ""

            for bio_url in bio.get("links") or []:
                host = bio_url.split("//")[-1].split("/")[0].lower().replace("www.", "")
                if not aggregator_from_bio and is_link_aggregator(bio_url):
                    aggregator_from_bio = bio_url
                # динамически определяем ключ соцсети по конфигу
                key = None
                for h, kk in _HOST_TO_KEY.items():
                    if host == h or host.endswith("." + h):
                        key = kk
                        break
                if (
                    key
                    and key in main_data["socialLinks"]
                    and not main_data["socialLinks"][key]
                ):
                    main_data["socialLinks"][key] = bio_url

            # если агрегатор найден только в био - проверяем и мержим соцсети/контакты из него
            if (not aggregator_url) and aggregator_from_bio:
                tw = main_data["socialLinks"].get("twitterURL", "")
                m = re.match(
                    r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
                    (tw or "") + "/",
                    re.I,
                )
                handle = m.group(1) if m else None
                ok_belongs, verified_bits = verify_aggregator_belongs(
                    aggregator_from_bio, site_domain, handle
                )
                if ok_belongs:
                    # соцсети
                    socials_from_agg = (
                        extract_socials_from_aggregator(aggregator_from_bio) or {}
                    )
                    try:
                        _twlog = _get_logger("twitter")
                        _twlog.info(
                            "Агрегатор обогащение %s: %s",
                            aggregator_from_bio,
                            {k: v for k, v in socials_from_agg.items() if v},
                        )
                    except Exception:
                        pass
                    for k, v in socials_from_agg.items():
                        if k == "websiteURL" or not v:
                            continue
                        if (
                            k in main_data["socialLinks"]
                            and not main_data["socialLinks"][k]
                        ):
                            main_data["socialLinks"][k] = v
                    if verified_bits.get("websiteURL") and not main_data[
                        "socialLinks"
                    ].get("websiteURL"):
                        main_data["socialLinks"]["websiteURL"] = verified_bits[
                            "websiteURL"
                        ]

                    # контакты (email + persons)
                    try:
                        agg_contacts = extract_contacts_from_aggregator(aggregator_from_bio) or {}
                    except Exception:
                        agg_contacts = {}

                    # emails
                    if agg_contacts.get("emails"):
                        main_data["contacts"]["emails"] = list(
                            dict.fromkeys([*main_data["contacts"]["emails"], *agg_contacts["emails"]])
                        )

                    # persons
                    existing = list(main_data["contacts"].get("persons") or [])
                    incoming = list(agg_contacts.get("persons") or [])

                    def _key(p: dict) -> tuple[str, str]:
                        return (
                            (p.get("role") or "").strip().lower(),
                            (
                                p.get("email")
                                or p.get("telegram")
                                or p.get("discord")
                                or p.get("linkedin")
                                or p.get("x")
                                or p.get("website")
                                or ""
                            )
                            .strip()
                            .lower(),
                        )

                    bykey = {_key(p): p for p in existing if _key(p)}
                    for p in incoming:
                        k = _key(p)
                        if not k:
                            continue
                        if k in bykey:
                            # дополняем отсутствующие поля
                            for kk, vv in p.items():
                                if vv and not bykey[k].get(kk):
                                    bykey[k][kk] = vv
                        else:
                            existing.append(p)
                            bykey[k] = p

                    if existing:
                        main_data["contacts"]["persons"] = existing

            # аватар из X → сохраняем в storage/<project>.jpg
            real_avatar = avatar_verified or (
                bio.get("avatar") if isinstance(bio, dict) else ""
            )
            if real_avatar and main_data["socialLinks"].get("twitterURL"):
                project_slug = (
                    (brand_from_url(website_url) or "project").replace(" ", "").lower()
                )
                logo_filename = f"{project_slug}.jpg"
                saved = download_twitter_avatar(
                    avatar_url=real_avatar,
                    twitter_url=main_data["socialLinks"]["twitterURL"],
                    storage_dir=storage_path,
                    filename=logo_filename,
                )
                if saved:
                    main_data["svgLogo"] = logo_filename

            # имя проекта: берем с сайта; если пусто - пробуем display name из X
            try:
                parsed_name = extract_project_name(
                    html, website_url, twitter_display_name=twitter_display
                )
                if parsed_name:
                    main_data["name"] = parsed_name
            except Exception:
                pass

        except Exception as e:
            logger.warning("Twitter BIO/avatar block failed: %s", e)

        # youtube: embed, handle, title (если есть)
        yt = main_data["socialLinks"].get("youtubeURL", "")
        if yt:
            try:
                embed = youtube_watch_to_embed(yt)
                if embed:
                    main_data["youtubeEmbed"] = embed
                handle = youtube_to_handle(yt)
                if handle:
                    main_data["youtubeHandle"] = handle
                title = youtube_oembed_title(yt)
                if title:
                    main_data["youtubeTitle"] = title
            except Exception as e:
                logger.warning("YouTube enrich error: %s", e)

    except Exception as e:
        logger.error("collect_main_data crash: %s\n%s", e, traceback.format_exc())

    # финальная нормализация + форс https
    main_data["socialLinks"] = normalize_socials(main_data.get("socialLinks", {}))
    for k, v in list(main_data["socialLinks"].items()):
        if isinstance(v, str) and v:
            main_data["socialLinks"][k] = force_https(v)

    # Хард-чек: twitterURL строго https://x.com/<handle>
    tw = main_data["socialLinks"].get("twitterURL", "")
    if isinstance(tw, str) and tw:
        tw_canon = twitter_to_x(tw)
        main_data["socialLinks"]["twitterURL"] = tw_canon

    tw = main_data["socialLinks"].get("twitterURL", "")
    if isinstance(tw, str) and tw:
        if not re.match(r"^https?://(?:www\.)?x\.com/[A-Za-z0-9_]{1,15}$", tw, re.I):
            main_data["socialLinks"]["twitterURL"] = ""

    logger.info(
        "Конечный результат %s: %s",
        website_url,
        {k: v for k, v in main_data["socialLinks"].items() if v},
    )
    return main_data
