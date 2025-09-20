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


# Helper: нормализуем person-словарь агрегатора в новый формат contacts.people
def _person_from_channels(src: dict) -> dict:
    name = (src.get("name") or "").strip()
    role = (src.get("role") or "").strip()
    person = {
        "name": name,
        "position": role,
        "emails": [src["email"]] if src.get("email") else [],
        "phones": [],
        "links": {
            "linkedin": src.get("linkedin") or "",
            "twitter": (src.get("x") or "").strip(),
            "telegram": src.get("telegram") or "",
            "discord": src.get("discord") or "",
            "website": src.get("website") or "",
        },
        "notes": f"sourced: {src.get('source') or 'aggregator'}",
    }
    # очистка пустых ссылок
    for k, v in list(person["links"].items()):
        if not v:
            person["links"][k] = ""
    return person


# Helper: ключ для дедупликации персон по основному каналу + роли
def _person_key_for_dedup(src: dict) -> tuple[str, str]:
    role = (src.get("role") or src.get("position") or "").strip().lower()
    main = (
        src.get("email")
        or (src.get("links") or {}).get("telegram")
        or (src.get("links") or {}).get("discord")
        or (src.get("links") or {}).get("linkedin")
        or (src.get("links") or {}).get("twitter")
        or (src.get("links") or {}).get("website")
        or src.get("telegram")
        or src.get("discord")
        or src.get("linkedin")
        or src.get("x")
        or src.get("website")
        or ""
    )
    return role, (main or "").strip().lower()


# Helper: собрать host→ключ соцсети из настроек (короткие имена ключей)
def _build_host_key_map(get_social_hosts) -> dict:
    key_map: dict[str, str] = {}
    for h in get_social_hosts():
        if h in ("x.com", "twitter.com") or "twitter" in h or h.endswith(".x.com"):
            key_map[h] = "twitter"
        elif "discord" in h:
            key_map[h] = "discord"
        elif h == "t.me" or "telegram" in h:
            key_map[h] = "telegram"
        elif h == "youtu.be" or "youtube" in h:
            key_map[h] = "youtube"
        elif h == "lnkd.in" or "linkedin" in h:
            key_map[h] = "linkedin"
        elif "reddit" in h:
            key_map[h] = "reddit"
        elif "medium" in h:
            key_map[h] = "medium"
        elif "github" in h:
            key_map[h] = "github"
    return key_map


# Entrypoint: собираем main.json-подобную структуру по сайту (короткие ключи)
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
    # локальные импорты, завязанные на рантайм-конфиг
    from core.parser.link_aggregator import extract_contacts_from_aggregator
    from core.settings import get_social_hosts  # для динамического host→key

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

    # socialLinks (только короткие ключи) и обязательный website
    website_url = force_https(website_url)
    main_data["socialLinks"] = {k: "" for k in social_keys}
    main_data["socialLinks"]["website"] = website_url

    # остальные поля каркаса (новая схема contacts.support / contacts.people)
    main_data.setdefault("name", "")
    main_data.setdefault("contacts", {})
    main_data["contacts"].setdefault(
        "support",
        {
            "email": [],
            "phone": [],
            "twitter": [],
            "telegram": [],
            "discord": [],
            "linkedin": [],
            "website": [],
            "forms": [],
        },
    )
    main_data["contacts"].setdefault("people", [])

    # подготовим маппинг host→ключ
    _HOST_TO_KEY = _build_host_key_map(get_social_hosts)

    try:
        # загрузка главной (auto: requests → playwright при необходимости)
        html = fetch_url_html(website_url, prefer="auto")

        # первичные соцссылки/доки с главной
        socials = extract_social_links(html, website_url, is_main_page=True)
        socials = normalize_socials(socials)  # уже короткие ключи

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

        # контакты с сайта → support.{email/forms}
        site_contacts = extract_contacts_from_site(html, website_url)
        if site_contacts.get("emails"):
            main_data["contacts"]["support"]["email"] = list(
                dict.fromkeys(
                    [
                        *main_data["contacts"]["support"]["email"],
                        *site_contacts["emails"],
                    ]
                )
            )
        if site_contacts.get("forms"):
            main_data["contacts"]["support"]["forms"] = list(
                dict.fromkeys(
                    [
                        *main_data["contacts"]["support"]["forms"],
                        *site_contacts["forms"],
                    ]
                )
            )

        # контакты из GitHub (email) - source для support.email
        gh = main_data["socialLinks"].get("github") or ""
        if gh:
            gh_contacts = extract_contacts_from_github(gh)
            if gh_contacts.get("emails"):
                main_data["contacts"]["support"]["email"] = list(
                    dict.fromkeys(
                        [
                            *main_data["contacts"]["support"]["email"],
                            *gh_contacts["emails"],
                        ]
                    )
                )

        # разбор X/Twitter: выбор верифицированного, домерж через агрегатор, аватар
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""
        twitter_final = ""
        enriched_from_agg = {}
        aggregator_url = ""
        avatar_verified = ""

        try:
            # выбираем правильный twitter из кандидатов
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
                main_data["socialLinks"]["twitter"] = twitter_final

            # домерж соцсетей, полученных из агрегатора твиттера (кроме website)
            for k, v in (enriched_from_agg or {}).items():
                if k == "website" or not v:
                    continue
                if k in main_data["socialLinks"] and not main_data["socialLinks"][k]:
                    main_data["socialLinks"][k] = v

        except Exception as e:
            logger.warning("Twitter verification error: %s", e)

        try:
            # BIO/аватар X + возможный линк-агрегатор в BIO
            bio = {}
            avatar_url = avatar_verified or ""
            need_bio_for_avatar = bool(
                main_data["socialLinks"].get("twitter") and (not avatar_url)
            )

            # display name из X (без запроса аватара)
            twitter_display = ""
            if main_data["socialLinks"].get("twitter"):
                try:
                    tw_profile = (
                        get_links_from_x_profile(
                            main_data["socialLinks"]["twitter"], need_avatar=False
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
                            main_data["socialLinks"]["twitter"], need_avatar=True
                        )
                        or {}
                    )
                except Exception:
                    bio = {}

            # собрать из bio все ссылки + пометить агрегатор
            aggregator_from_bio = ""

            for bio_url in bio.get("links") or []:
                host = bio_url.split("//")[-1].split("/")[0].lower().replace("www.", "")
                if not aggregator_from_bio and is_link_aggregator(bio_url):
                    aggregator_from_bio = bio_url
                # динамический подбор ключа соцсети по домену
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

            # если агрегатор найден только в bio - проверяем и мержим соцсети/контакты
            if (not aggregator_url) and aggregator_from_bio:
                tw = main_data["socialLinks"].get("twitter", "")
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
                        if k == "website" or not v:
                            continue
                        if (
                            k in main_data["socialLinks"]
                            and not main_data["socialLinks"][k]
                        ):
                            main_data["socialLinks"][k] = v

                    # если агрегатор дал официальный сайт — заполним, если пусто
                    if verified_bits.get("website") and not main_data[
                        "socialLinks"
                    ].get("website"):
                        main_data["socialLinks"]["website"] = verified_bits["website"]

                    # контакты (emails + persons) из агрегатора → support/people
                    try:
                        agg_contacts = (
                            extract_contacts_from_aggregator(aggregator_from_bio) or {}
                        )
                    except Exception:
                        agg_contacts = {}

                    # emails → support.email
                    if agg_contacts.get("emails"):
                        main_data["contacts"]["support"]["email"] = list(
                            dict.fromkeys(
                                [
                                    *main_data["contacts"]["support"]["email"],
                                    *agg_contacts["emails"],
                                ]
                            )
                        )

                    # persons → contacts.people (конвертация в новый формат)
                    existing_people = list(main_data["contacts"].get("people") or [])
                    # привести уже существующее к ключам дедупликации
                    existing_index = {
                        _person_key_for_dedup(p): p for p in existing_people if p
                    }

                    for p in agg_contacts.get("persons") or []:
                        norm = _person_from_channels(p)
                        k = _person_key_for_dedup(
                            {
                                "role": norm.get("position"),
                                "links": norm.get("links"),
                                "email": (norm.get("emails") or [None])[0],
                            }
                        )
                        if k in existing_index:
                            # дополним поля, не затирая уже заполненные
                            dst = existing_index[k]
                            if norm.get("name") and not dst.get("name"):
                                dst["name"] = norm["name"]
                            if norm.get("position") and not dst.get("position"):
                                dst["position"] = norm["position"]
                            # emails
                            dst_emails = set(dst.get("emails") or [])
                            for e in norm.get("emails") or []:
                                if e and e not in dst_emails:
                                    dst_emails.add(e)
                            dst["emails"] = list(dst_emails)
                            # links
                            dst_links = dst.get("links") or {}
                            for lk, lv in (norm.get("links") or {}).items():
                                if lv and not (dst_links.get(lk) or "").strip():
                                    dst_links[lk] = lv
                            dst["links"] = dst_links
                        else:
                            existing_people.append(norm)
                            existing_index[k] = norm

                    main_data["contacts"]["people"] = existing_people

            # аватар из X → сохраняем в storage/<project>.jpg
            real_avatar = avatar_verified or (
                bio.get("avatar") if isinstance(bio, dict) else ""
            )
            if real_avatar and main_data["socialLinks"].get("twitter"):
                project_slug = (
                    (brand_from_url(website_url) or "project").replace(" ", "").lower()
                )
                logo_filename = f"{project_slug}.jpg"
                saved = download_twitter_avatar(
                    avatar_url=real_avatar,
                    twitter_url=main_data["socialLinks"]["twitter"],
                    storage_dir=storage_path,
                    filename=logo_filename,
                )
                if saved:
                    main_data["svgLogo"] = logo_filename

            # имя проекта: сайт → если пусто, возьмем display name из X
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
        yt = main_data["socialLinks"].get("youtube", "")
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

    # финальная нормализация + форс https (все соцсети — короткие ключи)
    main_data["socialLinks"] = normalize_socials(main_data.get("socialLinks", {}))
    for k, v in list(main_data["socialLinks"].items()):
        if isinstance(v, str) and v:
            main_data["socialLinks"][k] = force_https(v)

    # Хард-чек: twitter строго https://x.com/<handle>
    tw = main_data["socialLinks"].get("twitter", "")
    if isinstance(tw, str) and tw:
        main_data["socialLinks"]["twitter"] = twitter_to_x(tw)

    tw = main_data["socialLinks"].get("twitter", "")
    if isinstance(tw, str) and tw:
        if not re.match(r"^https?://(?:www\.)?x\.com/[A-Za-z0-9_]{1,15}$", tw, re.I):
            main_data["socialLinks"]["twitter"] = ""

    logger.info(
        "Конечный результат %s: %s",
        website_url,
        {k: v for k, v in main_data["socialLinks"].items() if v},
    )
    return main_data
