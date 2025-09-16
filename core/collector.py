from __future__ import annotations

import copy
import json as _json
import re
import traceback

# Логгер
from core.log_setup import get_logger

# Нормализация ссылок/бренда
from core.normalize import brand_from_url, force_https, normalize_socials, twitter_to_x

# Работа с линк-агрегаторами
from core.parser.link_aggregator import is_link_aggregator

# Парсер X/Twitter: выбор верифицированного, bio-ссылки, аватар
from core.parser.twitter import (
    download_twitter_avatar,
    get_links_from_x_profile,
    reset_verified_state,
    select_verified_twitter,
)

# Веб-парсер: загрузка HTML, извлечение соцсетей и имени проекта
from core.parser.web import (
    extract_project_name,
    extract_social_links,
    fetch_url_html,
    get_domain_name,
)

# YouTube-хелперы
from core.parser.youtube import (
    youtube_oembed_title,
    youtube_to_handle,
    youtube_watch_to_embed,
)
from core.paths import PROJECT_ROOT

logger = get_logger("collector")


# Основная точка: сбор main.json-подобной структуры по сайту
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
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
    main_data.setdefault("shortDescription", "")
    main_data.setdefault("contentMarkdown", "")
    main_data.setdefault("seo", {})
    main_data.setdefault("coinData", {})
    main_data["videoSlider"] = main_data.get("videoSlider", [])
    main_data["svgLogo"] = main_data.get("svgLogo", "")

    try:
        # грузим главную страницу проекта (auto: requests → при необходимости Playwright)
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

        logger.info(
            "Начальное обогащение %s: %s",
            website_url,
            {k: v for k, v in main_data["socialLinks"].items() if v},
        )

        # разбор X/Twitter: выбор верифицированного, домерж из агрегатора, аватар
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""
        twitter_final = ""
        enriched_from_agg = {}
        aggregator_url = ""
        avatar_verified = ""

        try:
            # выбираем «правильный» twitter из найденных кандидатов
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

            # cобираем из bio все встреченные ссылки + помечаем агрегатор
            aggregator_from_bio = ""
            mapping = {
                "x.com": "twitterURL",
                "twitter.com": "twitterURL",
                "t.me": "telegramURL",
                "telegram.me": "telegramURL",
                "discord.gg": "discordURL",
                "discord.com": "discordURL",
                "youtube.com": "youtubeURL",
                "youtu.be": "youtubeURL",
                "medium.com": "mediumURL",
                "github.com": "githubURL",
                "linkedin.com": "linkedinURL",
                "reddit.com": "redditURL",
            }

            for bio_url in bio.get("links") or []:
                host = bio_url.split("//")[-1].split("/")[0].lower().replace("www.", "")
                if not aggregator_from_bio and is_link_aggregator(bio_url):
                    aggregator_from_bio = bio_url
                k = mapping.get(host)
                if k in main_data["socialLinks"] and not main_data["socialLinks"][k]:
                    main_data["socialLinks"][k] = bio_url

            # если агрегатор найден только в био - проверяем и мержим соцсети из него
            if (not aggregator_url) and aggregator_from_bio:
                from core.parser.link_aggregator import (
                    extract_socials_from_aggregator,
                    verify_aggregator_belongs,
                )

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
                    socials_from_agg = (
                        extract_socials_from_aggregator(aggregator_from_bio) or {}
                    )
                    # единообразный лог в стиле twitter.py
                    try:
                        from core.log_setup import get_logger as _get_logger

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
