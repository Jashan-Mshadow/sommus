"""Gmail over SMTP and IMAP, authenticated with an app password.

Chosen over the Gmail API on purpose: Gmail's read scopes are "restricted", so a
personal OAuth app can't leave Testing mode without a security assessment, and
tokens in Testing expire every 7 days. An app password never expires.

Credentials come from .env: GMAIL_ADDRESS and GMAIL_APP_PASSWORD.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import time
from contextlib import contextmanager
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'
DRAFTS = '"[Gmail]/Drafts"'
MAX_BODY_CHARS = 8_000

SETUP_HELP = (
    "Gmail isn't connected. Turn on 2-Step Verification, create an app password at "
    "myaccount.google.com/apppasswords, then put GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env."
)


class MailError(Exception):
    """An expected failure, phrased for the user."""


def account() -> tuple[str, str]:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise MailError(SETUP_HELP)
    return address, password


def configured() -> bool:
    return bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))


@contextmanager
def imap(folder: str = ALL_MAIL, readonly: bool = True):
    address, password = account()
    try:
        connection = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)
        connection.login(address, password)
    except imaplib.IMAP4.error as e:
        raise MailError(
            f"Gmail rejected the sign-in ({e}). Check GMAIL_APP_PASSWORD in .env — it must be an "
            "app password, not the account password."
        ) from e
    except OSError as e:
        raise MailError(f"Couldn't reach Gmail: {e}") from e
    try:
        connection.select(folder, readonly=readonly)
        yield connection
    finally:
        try:
            connection.close()
        except Exception:
            pass
        connection.logout()


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def _uids(connection, query: str, limit: int) -> list[bytes]:
    # X-GM-RAW is Gmail's own search syntax over IMAP: is:unread, from:x, newer_than:2d...
    status, data = connection.uid("SEARCH", "X-GM-RAW", f'"{query}"')
    if status != "OK":
        raise MailError(f"Gmail couldn't run that search: {query}")
    return data[0].split()[::-1][:limit]  # newest first


def search(query: str = "in:inbox", limit: int = 10) -> list[str]:
    with imap() as connection:
        uids = _uids(connection, query, limit)
        if not uids:
            return []
        lines = []
        for uid in uids:
            status, data = connection.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            flags = data[0][0].decode(errors="replace")
            headers = email.message_from_bytes(data[0][1])
            unread = "\\Seen" not in flags
            lines.append(
                f"[{uid.decode()}] {'• ' if unread else ''}{_decode(headers.get('From'))} — "
                f"{_decode(headers.get('Subject'))} ({_decode(headers.get('Date'))})"
            )
        return lines


def _body(message: email.message.Message) -> str:
    html = ""
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except (AttributeError, LookupError):
            continue
        if part.get_content_subtype() == "plain" and text.strip():
            return text
        html = html or text
    if html:
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        return re.sub(r"[ \t]{2,}", " ", stripped)
    return ""


def fetch(uid: str) -> tuple[email.message.Message, str]:
    with imap() as connection:
        status, data = connection.uid("FETCH", uid, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise MailError(f"No message with id {uid}.")
        message = email.message_from_bytes(data[0][1])
    body = _body(message).strip()
    return message, body[:MAX_BODY_CHARS] + ("\n[truncated]" if len(body) > MAX_BODY_CHARS else "")


def mark_read(uid: str) -> None:
    with imap(readonly=False) as connection:
        connection.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def send(message: EmailMessage) -> None:
    address, password = account()
    message["From"] = address
    message["Date"] = formatdate(localtime=True)
    message.setdefault("Message-ID", make_msgid())
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)  # Gmail files SMTP-sent mail in Sent automatically
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"Gmail rejected the sign-in: {e}. GMAIL_APP_PASSWORD must be an app password.") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Couldn't send: {e}") from e


def save_draft(message: EmailMessage) -> None:
    address, password = account()
    message["From"] = address
    message["Date"] = formatdate(localtime=True)
    try:
        connection = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)
        connection.login(address, password)
        connection.append(DRAFTS, "\\Draft", imaplib.Time2Internaldate(time.time()), message.as_bytes())
        connection.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailError(f"Couldn't save the draft: {e}") from e


def build_reply(original: email.message.Message, body: str, reply_all: bool) -> EmailMessage:
    reply = EmailMessage()
    reply["To"] = original.get("Reply-To") or original.get("From")
    if reply_all and original.get("Cc"):
        me = parseaddr(os.environ.get("GMAIL_ADDRESS", ""))[1].casefold()
        others = [a for a in original.get("Cc").split(",") if parseaddr(a)[1].casefold() != me]
        if others:
            reply["Cc"] = ", ".join(a.strip() for a in others)
    subject = _decode(original.get("Subject"))
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original.get("Message-ID"):
        reply["In-Reply-To"] = original["Message-ID"]
        reply["References"] = f"{original.get('References', '')} {original['Message-ID']}".strip()
    reply.set_content(body)
    return reply


def decode_header_value(value: str | None) -> str:
    return _decode(value)
