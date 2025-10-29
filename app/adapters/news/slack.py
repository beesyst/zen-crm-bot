from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple

import httpx

LOG = logging.getLogger(__name__)


# Текущее время в UTC (для oldest и дефолтных меток)
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Сформировать заголовки авторизации для Slack Web API
def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Получить карту каналов: name -> (id, normalized_name)
def _channels_map(client: httpx.Client, api_base: str) -> Dict[str, Tuple[str, str]]:
    out: Dict[str, Tuple[str, str]] = {}
    cursor = None
    while True:
        params = {
            "limit": 1000,
            "exclude_archived": True,
            "types": "public_channel,private_channel",
        }
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{api_base}/conversations.list", params=params)
        data = r.json()
        if not data.get("ok"):
            LOG.warning("conversations.list failed: %s", data)
            break
        for ch in data.get("channels", []) or []:
            name = (ch.get("name") or "").strip()
            cid = ch.get("id") or ""
            if name and cid:
                out[name] = (cid, name)
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


# Итерировать историю одного канала (и опционально треды)
def _history(
    client: httpx.Client,
    api_base: str,
    channel_id: str,
    oldest_ts: float,
    thread_replies: bool,
    max_batches: int = 10,
) -> Iterable[Dict[str, Any]]:
    cursor = None
    batches = 0
    while True:
        params = {
            "channel": channel_id,
            "limit": 200,
            "oldest": f"{oldest_ts:.6f}",
            "inclusive": True,
        }
        if cursor:
            params["cursor"] = cursor

        r = client.get(f"{api_base}/conversations.history", params=params)
        data = r.json()
        if not data.get("ok"):
            LOG.warning("conversations.history failed: %s", data)
            break

        for m in data.get("messages") or []:
            # базовое сообщение
            yield m

            # при необходимости - догрузим ответы треда
            if thread_replies and m.get("thread_ts"):
                ts = m.get("thread_ts")
                cursor2 = None
                while True:
                    params2 = {
                        "channel": channel_id,
                        "ts": ts,
                        "limit": 200,
                        "oldest": f"{oldest_ts:.6f}",
                        "inclusive": True,
                    }
                    if cursor2:
                        params2["cursor"] = cursor2
                    r2 = client.get(f"{api_base}/conversations.replies", params=params2)
                    d2 = r2.json()
                    if not d2.get("ok"):
                        LOG.warning("conversations.replies failed: %s", d2)
                        break
                    replies = d2.get("messages") or []
                    # нулевой элемент - сам parent; пропускаем
                    for i, rm in enumerate(replies):
                        if i == 0:
                            continue
                        yield rm
                    cursor2 = (d2.get("response_metadata") or {}).get(
                        "next_cursor"
                    ) or ""
                    if not cursor2:
                        break

        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        batches += 1
        if not cursor or batches >= max_batches:
            break


# Получить постоянную ссылку на сообщение (chat.getPermalink)
def _permalink(client: httpx.Client, api_base: str, channel_id: str, ts: str) -> str:
    r = client.get(
        f"{api_base}/chat.getPermalink",
        params={"channel": channel_id, "message_ts": ts},
    )
    d = r.json()
    if not d.get("ok"):
        LOG.debug("chat.getPermalink failed for %s:%s -> %s", channel_id, ts, d)
    return d.get("permalink") or ""


# Преобразовать slack-ts (строка "1234567890.000") в float
def _float_ts(ts: Any) -> float:
    try:
        return float(str(ts))
    except Exception:
        return _utcnow().timestamp()


# Сжать текст в заголовок до limit
def _short_title(text: str, limit: int = 120) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:limit].rstrip() if t else "Slack message"


# Главная точка входа адаптера: прочитать YAML-конфиг проекта и собрать элементы
def pull(project_key: str, app_cfg: dict) -> List[Dict[str, Any]]:
    """
    Читаем токен и базовый URL из config/apps/<project>.yml:
      sources.slack.api_base
      sources.slack.bot_token
    Никаких ENV и хардкодов.
    """
    src_cfg = (app_cfg.get("sources") or {}).get("slack") or {}

    api_base = (src_cfg.get("api_base") or "").strip().rstrip("/")
    if not api_base:
        # строго: без api_base не работаем (централизация конфигов)
        LOG.warning(
            "Slack api_base is missing in app config for project=%s", project_key
        )
        return []

    token = (src_cfg.get("bot_token") or "").strip()
    if not token:
        LOG.warning(
            "Slack bot_token is missing in app config for project=%s", project_key
        )
        return []

    # каналы допускаются как имена, так и явные ID (C.../G...)
    want_channels = [
        c.strip() for c in (src_cfg.get("channels") or []) if c and str(c).strip()
    ]
    thread_replies = bool(src_cfg.get("thread_replies", False))
    oldest_days = int(src_cfg.get("oldest_days") or 7)
    oldest_ts = (_utcnow() - timedelta(days=oldest_days)).timestamp()

    out: List[Dict[str, Any]] = []
    tags = list(app_cfg.get("tags") or [])
    visibility = app_cfg.get("visibility_hint") or None

    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = _auth_headers(token)

    with httpx.Client(timeout=timeout, headers=headers) as client:
        # резолвим имена каналов в ID
        cmap = _channels_map(client, api_base)

        # подготовим цели: (channel_id, printable_name)
        targets: List[Tuple[str, str]] = []
        for ch in want_channels:
            if ch.startswith(("C", "G")):
                targets.append((ch, ch))
            elif ch in cmap:
                targets.append((cmap[ch][0], cmap[ch][1]))
        if not targets:
            LOG.info("No matching Slack channels for project=%s", project_key)
            return []

        # идем по каналам и собираем сообщения
        for channel_id, printable in targets:
            for m in _history(client, api_base, channel_id, oldest_ts, thread_replies):
                ts = str(m.get("ts") or "")
                text = (m.get("text") or "").strip()
                user = (m.get("user") or m.get("username") or "").strip()
                thread_ts = str(m.get("thread_ts") or "")

                link = _permalink(client, api_base, channel_id, ts) if ts else ""

                # мин набор - раннер + Pydantic довалидируют/дочистят
                item = {
                    "id": (
                        f"slack:{printable}:{ts}"
                        if ts
                        else f"slack:{printable}:{_utcnow().timestamp()}"
                    ),
                    "source": "slack",
                    "project_key": project_key,
                    "title": _short_title(text),
                    "body": text,
                    "url": link,
                    "author": user,
                    "channel": printable,
                    "thread_ts": thread_ts,
                    "ts": _float_ts(ts),
                    "tags": tags,
                    "visibility": visibility,
                }
                out.append(item)

    return out
