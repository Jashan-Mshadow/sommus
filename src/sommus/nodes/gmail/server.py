"""Gmail node: read, search, send and reply, with no browser window involved.

The browser route (`compose_email` on the laptop node) still exists and needs no
setup; this one works when nobody is at the keyboard, which is the point.
"""

from __future__ import annotations

import base64
import functools
from collections.abc import Callable
from email.message import EmailMessage

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sommus.nodes.gmail.auth import GmailAuthError, service

READ = ToolAnnotations(read_only_hint=True)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

MAX_BODY_CHARS = 8_000

server = MCPServer(
    "sommus-gmail",
    instructions="Reads and sends Jashan's Gmail (jashandeepm2008@gmail.com) directly through the Gmail API.",
    log_level="WARNING",
)


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except GmailAuthError as e:
                raise ToolError(str(e)) from e
            except Exception as e:  # API errors carry useful detail; don't swallow them
                raise ToolError(f"Gmail error: {e}") from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


def _header(message: dict, name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header["name"].casefold() == name.casefold():
            return header["value"]
    return ""


def _body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripping the HTML part."""
    if payload.get("mimeType", "").startswith("text/") and payload.get("body", {}).get("data"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
        if payload["mimeType"] == "text/html":
            import re

            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"[ \t]{2,}", " ", raw)
        return raw
    for part in payload.get("parts", []):
        text = _body_text(part)
        if text.strip():
            return text
    return ""


@tool(READ)
def search_email(query: str = "in:inbox", limit: int = 10) -> str:
    """Search the mailbox using Gmail's own search syntax.

    Args:
        query: e.g. "is:unread", "from:prof@uwaterloo.ca", "subject:co-op newer_than:3d".
        limit: How many messages to list (default 10).
    """
    api = service()
    found = api.users().messages().list(userId="me", q=query, maxResults=limit).execute().get("messages", [])
    if not found:
        return f"No messages match '{query}'."
    lines = []
    for item in found:
        message = (
            api.users()
            .messages()
            .get(userId="me", id=item["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        unread = "UNREAD" in message.get("labelIds", [])
        lines.append(
            f"[{item['id']}] {'• ' if unread else ''}{_header(message, 'From')} — "
            f"{_header(message, 'Subject')} ({_header(message, 'Date')})"
        )
    return f"{len(lines)} messages for '{query}':\n" + "\n".join(lines)


@tool(READ)
def read_email(message_id: str) -> str:
    """Read one message in full.

    Args:
        message_id: The id in brackets from search_email.
    """
    api = service()
    message = api.users().messages().get(userId="me", id=message_id, format="full").execute()
    body = _body_text(message.get("payload", {})).strip()
    return (
        f"From: {_header(message, 'From')}\nTo: {_header(message, 'To')}\n"
        f"Subject: {_header(message, 'Subject')}\nDate: {_header(message, 'Date')}\n\n"
        + (body[:MAX_BODY_CHARS] + ("\n[truncated]" if len(body) > MAX_BODY_CHARS else "") or "(no text body)")
    )


def _send(message: EmailMessage, thread_id: str | None = None) -> str:
    api = service()
    payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    sent = api.users().messages().send(userId="me", body=payload).execute()
    return sent["id"]


@tool(REVERSIBLE)
def send_email(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Send an email straight from Jashan's Gmail — no browser, no draft to approve.

    Args:
        to: Recipient address (comma-separated for several).
        subject: Subject line.
        body: Plain-text message.
        cc: Optional CC addresses.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    message.set_content(body)
    return f"Sent to {to} (id {_send(message)})."


@tool(REVERSIBLE)
def reply_to_email(message_id: str, body: str, reply_all: bool = False) -> str:
    """Reply in the same thread as an existing message.

    Args:
        message_id: The message being replied to.
        body: Plain-text reply.
        reply_all: True to include everyone on the thread.
    """
    api = service()
    original = (
        api.users()
        .messages()
        .get(
            userId="me", id=message_id, format="metadata", metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID"]
        )
        .execute()
    )
    subject = _header(original, "Subject")
    message = EmailMessage()
    message["To"] = _header(original, "From")
    if reply_all and _header(original, "Cc"):
        message["Cc"] = _header(original, "Cc")
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message["In-Reply-To"] = message["References"] = _header(original, "Message-ID")
    message.set_content(body)
    return f"Replied to {_header(original, 'From')} (id {_send(message, original.get('threadId'))})."


@tool(REVERSIBLE)
def draft_email(to: str, subject: str, body: str) -> str:
    """Save a draft in Gmail without sending it.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Plain-text message.
    """
    api = service()
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    draft = api.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return f"Draft saved for {to} (id {draft['id']})."


@tool(REVERSIBLE)
def mark_email_read(message_id: str) -> str:
    """Mark a message as read.

    Args:
        message_id: The message to mark.
    """
    service().users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    return "Marked as read."


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
