from __future__ import annotations

import copy
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
from core.settings import get_social_host_map, get_social_keys

logger = _get_logger("collector")


# Хелпер: собрать маппинг host→ключ соцсети из конфигурации
def _host_to_social_key() -> dict:
    return get_social_host_map()


# Хелпер: инициализировать contacts.support на основе конфиг-ключей соцсетей
def _init_support_section() -> dict:
    support = {"email": [], "phone": [], "forms": []}
    for k in get_social_keys():
        support.setdefault(k, [])
    return support


# Хелпер: нормализуем словарь персоны агрегатора в формат contacts.people (ключи из конфига)
def _person_from_channels(src: dict) -> dict:
    name = (src.get("name") or "").strip()
    role = (src.get("role") or "").strip()

    allowed = get_social_keys()
    links = {k: "" for k in allowed}

    # заполняем ссылки строго по ключам из конфига (без алиасов/костылей)
    for k in allowed:
        v = src.get(k)
        if isinstance(v, str) and v.strip():
            links[k] = v.strip()

    person = {
        "name": name,
        "position": role,
        "emails": (
            [src["email"].strip()]
            if isinstance(src.get("email"), str) and src.get("email").strip()
            else []
        ),
        "phones": [],
        "links": links,
        "notes": f"sourced: {src.get('source') or 'aggregator'}",
    }
    return person


# Хелпер: ключ для дедупликации персон — роль + главный канал в порядке из конфига (email в приоритете)
def _person_key_for_dedup(src: dict) -> tuple[str, str]:
    role = (src.get("role") or src.get("position") or "").strip().lower()

    # email как главный идентификатор
    if isinstance(src.get("email"), str) and src.get("email").strip():
        return role, src.get("email").strip().lower()

    # далее — первый непустой канал в порядке socials.keys (только по links)
    keys_priority = get_social_keys()
    links = src.get("links") or {}
    for k in keys_priority:
        v = links.get(k)
        if isinstance(v, str) and v.strip():
            return role, v.strip().lower()

    return role, ""


# Хелпер: объединить ключи соцсетей из конфига и шаблона (для совместимости)
def _collect_social_keys_from_config_and_template(main_template: dict) -> list[str]:
    cfg_keys = get_social_keys()
    tmpl_keys = (
        list((main_template.get("socialLinks") or {}).keys())
        if isinstance(main_template, dict)
        else []
    )
    # конфиг — источник истины; ключи из шаблона добавляем в конец, без дублей
    return list(dict.fromkeys([*cfg_keys, *tmpl_keys]))


# Entrypoint: собираем main.json-подобную структуру по сайту (соц-ключи и host-map из конфига)
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
    # локальный импорт: контакты из агрегатора
    from core.parser.link_aggregator import extract_contacts_from_aggregator

    reset_verified_state(full=False)

    # Ключи соцсетей: конфиг ∪ (опционально) ключи шаблона
    social_keys = _collect_social_keys_from_config_and_template(main_template)

    main_data = copy.deepcopy(main_template) if isinstance(main_template, dict) else {}

    # socialLinks (короткие ключи) и обязательный website
    website_url = force_https(website_url)
    main_data["socialLinks"] = {k: "" for k in social_keys}
    main_data["socialLinks"]["website"] = website_url

    # каркас contacts: support/people управляется конфиг-ключами
    main_data.setdefault("name", "")
    main_data.setdefault("contacts", {})
    main_data["contacts"].setdefault("support", _init_support_section())
    main_data["contacts"].setdefault("people", [])

    # маппинг host→ключ из конфига
    host_map = _host_to_social_key()

    try:
        # загрузка главной (auto: requests → playwright при необходимости)
        html = fetch_url_html(website_url, prefer="auto")

        # первичные соцссылки/доки с главной
        socials = extract_social_links(html, website_url, is_main_page=True)
        socials = normalize_socials(socials)  # уже короткие ключи

        # перенос найденных соцсетей по списку ключей
        for k in social_keys:
            v = socials.get(k)
            if isinstance(v, str) and v.strip():
                main_data["socialLinks"][k] = v.strip()
        # если парсер вернул неизвестные ключи - добавим их
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
            # BIO/аватар X + возможный линк-агрегатор в bio
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

                # подобрать ключ соцсети по маппингу host_map
                key = host_map.get(host)
                if not key:
                    for base, social_key in host_map.items():
                        if host.endswith("." + base):
                            key = social_key
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
                    # соцсети с агрегатора
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

                    # офсайт из агрегатора - заполняем если пусто
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

                    if agg_contacts.get("emails"):
                        main_data["contacts"]["support"]["email"] = list(
                            dict.fromkeys(
                                [
                                    *main_data["contacts"]["support"]["email"],
                                    *agg_contacts["emails"],
                                ]
                            )
                        )

                    # persons → contacts.people (конвертация и дедуп по конфиг-приоритету)
                    existing_people = list(main_data["contacts"].get("people") or [])
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
                            dst = existing_index[k]
                            if norm.get("name") and not dst.get("name"):
                                dst["name"] = norm["name"]
                            if norm.get("position") and not dst.get("position"):
                                dst["position"] = norm["position"]
                            # emails merge
                            dst_emails = set(dst.get("emails") or [])
                            for e in norm.get("emails") or []:
                                if e and e not in dst_emails:
                                    dst_emails.add(e)
                            dst["emails"] = list(dst_emails)
                            # links merge
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

            # имя проекта: если пусто - возьмем display name из X как подсказку
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

    # финальная нормализация + форс https (все соцсети - короткие ключи)
    main_data["socialLinks"] = normalize_socials(main_data.get("socialLinks", {}))
    for k, v in list(main_data["socialLinks"].items()):
        if isinstance(v, str) and v:
            main_data["socialLinks"][k] = force_https(v)

    # строгая нормализация X: только https://x.com/<handle>
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
