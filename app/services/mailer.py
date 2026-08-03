"""Email delivery behind two interchangeable backends.

`outbox` (default) writes the real rendered HTML to disk and needs no
credentials, so the digest feature is demonstrable on a fresh clone. `smtp`
sends the identical message through a real server. Switching is an env change,
not a code change - the message construction is shared.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def _outbox_dir() -> Path:
    path = settings.resolve(settings.outbox_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "message"


def _build_message(to: str, subject: str, html: str, text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _html_to_text(html: str) -> str:
    """Plain-text alternative. Crude but real - a text/plain part that is just
    the raw markup is worse than none for deliverability."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</h[1-6]>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def send(to: str, subject: str, html: str) -> tuple[bool, str]:
    """Deliver a message. Returns (ok, detail) - never raises.

    A failed digest must not abort the scheduler run for every other user.
    """
    text = _html_to_text(html)

    try:
        if settings.email_backend == "smtp":
            return await _send_smtp(to, subject, html, text)
        return _send_outbox(to, subject, html)
    except Exception as exc:
        logger.warning("Email delivery to %s failed: %s", to, exc)
        return False, str(exc)[:300]


def _send_outbox(to: str, subject: str, html: str) -> tuple[bool, str]:
    # `__` separates the three fields because the timestamp and both slugs
    # contain single dashes; the inbox needs to recover the recipient exactly
    # in order to show a user only their own mail.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = _outbox_dir() / f"{_slug(to)}__{stamp}__{_slug(subject)}.html"
    path.write_text(html, encoding="utf-8")
    logger.info("Digest written to outbox: %s", path.name)
    return True, str(path)


async def _send_smtp(to: str, subject: str, html: str, text: str) -> tuple[bool, str]:
    if not settings.smtp_host:
        return False, "SMTP backend selected but SMTP_HOST is empty"

    import aiosmtplib

    message = _build_message(to, subject, html, text)
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user or None,
        password=settings.smtp_password or None,
        start_tls=settings.smtp_use_tls,
    )
    return True, f"smtp:{settings.smtp_host}"


def _describe(path: Path) -> dict:
    parts = path.stem.split("__")
    recipient_slug, subject_slug = (parts[0], parts[-1]) if len(parts) >= 3 else ("", path.stem)
    stat = path.stat()
    return {
        "name": path.name,
        "recipient_slug": recipient_slug,
        "subject": subject_slug.replace("-", " "),
        "sent_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(tzinfo=None),
        "size": stat.st_size,
    }


def list_outbox(limit: int = 20, *, recipient: str | None = None) -> list[dict]:
    """Recent outbox messages, for the in-app inbox view.

    `recipient` filters to one address so a signed-in user's inbox shows only
    their own mail - the dev outbox is a shared directory, and without this any
    user could read another user's digest.
    """
    directory = settings.resolve(settings.outbox_dir)
    if not directory.exists():
        return []

    wanted = _slug(recipient) if recipient else None
    files = sorted(directory.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)

    messages = []
    for path in files:
        info = _describe(path)
        if wanted and info["recipient_slug"] != wanted:
            continue
        messages.append(info)
        if len(messages) >= limit:
            break
    return messages


def read_outbox(name: str, *, recipient: str | None = None) -> str | None:
    """Read one outbox message by filename."""
    directory = settings.resolve(settings.outbox_dir)
    path = (directory / name).resolve()

    # Containment check: the filename comes from a URL, so a crafted "../"
    # must not read arbitrary files off disk.
    try:
        path.relative_to(directory.resolve())
    except ValueError:
        return None

    if not path.is_file() or path.suffix != ".html":
        return None

    # Same reason as list_outbox: the filename is user-supplied, so ownership
    # has to be checked here too and not just at listing time.
    if recipient and _describe(path)["recipient_slug"] != _slug(recipient):
        return None

    return path.read_text(encoding="utf-8")
