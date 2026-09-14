"""Gmail node: read, search, send and reply with no browser window involved.

Runs on SMTP and IMAP with an app password — see mail.py for why, not the Gmail API.
The browser route (`compose_email` on the laptop node) stays as the zero-setup fallback.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from email.message import EmailMessage

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sommus.nodes.gmail import mail

READ = ToolAnnotations(read_only_hint=True)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

server = MCPServer(
    "sommus-gmail",
    instructions="Reads and sends Jashan's Gmail over IMAP/SMTP.",
    log_level="WARNING",
)


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except mail.MailError as e:
                raise ToolError(str(e)) from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


@tool(READ)
def search_email(query: str = "in:inbox", limit: int = 10) -> str:
    """Search the mailbox using Gmail's own search syntax.

    Args:
        query: e.g. "is:unread", "from:prof@uwaterloo.ca", "subject:co-op newer_than:3d", "has:attachment".
        limit: How many messages to list, newest first (default 10).
    """
    lines = mail.search(query, limit)
    if not lines:
        return f"No messages match '{query}'."
    return f"{len(lines)} messages for '{query}' (• = unread):\n" + "\n".join(lines)


@tool(READ)
def read_email(message_id: str) -> str:
    """Read one message in full.

    Args:
        message_id: The id in brackets from search_email.
    """
    message, body = mail.fetch(message_id)
    header = mail.decode_header_value
    return (
        f"From: {header(message.get('From'))}\nTo: {header(message.get('To'))}\n"
        f"Subject: {header(message.get('Subject'))}\nDate: {header(message.get('Date'))}\n\n"
        + (body or "(no text body)")
    )


@tool(REVERSIBLE)
def send_email(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Send an email from Jashan's Gmail — no browser, no draft to approve.

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
    mail.send(message)
    return f"Sent to {to}."


@tool(REVERSIBLE)
def reply_to_email(message_id: str, body: str, reply_all: bool = False) -> str:
    """Reply in the same thread as an existing message.

    Args:
        message_id: The message being replied to, from search_email.
        body: Plain-text reply.
        reply_all: True to keep everyone else on the thread.
    """
    original, _ = mail.fetch(message_id)
    reply = mail.build_reply(original, body, reply_all)
    mail.send(reply)
    return f"Replied to {reply['To']}."


@tool(REVERSIBLE)
def draft_email(to: str, subject: str, body: str) -> str:
    """Save a draft in Gmail without sending it.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Plain-text message.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    mail.save_draft(message)
    return f"Draft saved for {to}."


@tool(REVERSIBLE)
def mark_email_read(message_id: str) -> str:
    """Mark a message as read.

    Args:
        message_id: The message to mark, from search_email.
    """
    mail.mark_read(message_id)
    return "Marked as read."


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
