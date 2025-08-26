from __future__ import annotations

import logging
import os
import smtplib
import time
from email.mime.text import MIMEText

log = logging.getLogger("infra.email")


def _smtp_settings():
    return {
        "host": os.getenv("SMTP_HOST"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "user": os.getenv("SMTP_USER"),
        "pass": os.getenv("SMTP_PASS"),
        "from": os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"),
    }


def send_email(to_email: str, subject: str, html: str, throttle_ms: int = 400):
    cfg = _smtp_settings()
    if not (cfg["host"] and cfg["user"] and cfg["pass"] and cfg["from"]):
        log.warning(
            "smtp not configured, dry-run",
            extra={"event": "email.dry_run", "to": to_email, "subject": subject},
        )
        return {"status": "dry-run"}

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to_email

    try:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"]) as s:
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(msg["From"], [to_email], msg.as_string())
        time.sleep(throttle_ms / 1000.0)  # троттлинг
        log.info(
            "email.sent",
            extra={"event": "email.sent", "to": to_email, "subject": subject},
        )
        return {"status": "sent"}
    except Exception as e:
        log.error(
            "email.error",
            extra={"event": "email.error", "to": to_email, "error": str(e)},
        )
        return {"status": "error", "error": str(e)}
