"""Read unread iMessages and texts from the Messages database (`~/Library/Messages/chat.db`), read-only.

Asking Messages through AppleScript can send but not read, so reading goes to the database. macOS only lets an
app open it with Full Disk Access (Terminal needs it, since Sommus runs there): without it the tool says exactly
what to switch on. Message text is other people's words, so the brain treats it as outside content.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sommus.nodes.laptop.macos import ActionError

CHAT_DB = Path("~/Library/Messages/chat.db").expanduser()
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
CLIP = 300
NEED_ACCESS = (
    "Can't open the Messages database. Give Terminal Full Disk Access (System Settings, Privacy & Security, "
    "Full Disk Access, switch on Terminal), then restart Sommus live."
)


def decode_body(blob: bytes | None) -> str:
    """The text of a message whose `text` column is empty: newer macOS keeps it inside an archived
    NSAttributedString. The string follows the 'NSString' marker, after a length byte (or 0x81 and two bytes)."""
    if not blob or b"NSString" not in blob:
        return ""
    rest = blob.split(b"NSString", 1)[1][5:]
    if not rest:
        return ""
    if rest[0] == 0x81:
        length, start = int.from_bytes(rest[1:3], "little"), 3
    else:
        length, start = rest[0], 1
    return rest[start : start + length].decode("utf-8", "replace").strip()


def _when(raw: int) -> datetime:
    seconds = raw / 1e9 if raw > 1e11 else raw  # nanoseconds on newer macOS, seconds on old ones
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone()


def unread(limit: int = 10, db_path: Path = CHAT_DB) -> list[dict]:
    """Unread messages from other people, newest first. Tapbacks and system events are left out."""
    if not db_path.exists():
        raise ActionError("There's no Messages database on this Mac.")
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = db.execute(
            """
            SELECT m.date, m.text, m.attributedBody, COALESCE(h.id, ''), COALESCE(c.display_name, '')
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
            LEFT JOIN chat c ON c.ROWID = j.chat_id
            WHERE m.is_from_me = 0 AND m.is_read = 0 AND m.item_type = 0 AND m.associated_message_type = 0
            ORDER BY m.date DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        db.close()
    except sqlite3.Error as e:
        raise ActionError(NEED_ACCESS) from e
    found = []
    for date, text, body, sender, group in rows:
        words = (text or decode_body(body)).strip()
        if words:
            found.append({"at": _when(date), "from": sender or "unknown", "group": group, "text": words})
    return found


def say(messages: list[dict]) -> str:
    if not messages:
        return "No unread messages."
    lines = [f"{len(messages)} unread, newest first:"]
    for m in messages:
        where = f" in {m['group']}" if m["group"] else ""
        text = m["text"] if len(m["text"]) <= CLIP else m["text"][: CLIP - 1] + "…"
        lines.append(f"- {m['from']}{where}, {m['at']:%a %-I:%M %p}: {text}")
    return "\n".join(lines)
