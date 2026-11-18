"""Unread iMessages from a Messages-shaped database (a small fake, no real chat.db)."""

import sqlite3
from datetime import UTC, datetime

import pytest

from sommus.nodes.laptop import macos, messages


def body(text: str) -> bytes:  # how newer macOS archives a message's text
    raw = text.encode()
    length = bytes([len(raw)]) if len(raw) < 0x81 else b"\x81" + len(raw).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85"
        b"\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + raw + b"\x86\x84\x02iI"
    )


def ns(moment: datetime) -> int:
    return int((moment - messages.APPLE_EPOCH).total_seconds() * 1e9)


@pytest.fixture
def chat_db(tmp_path):
    path = tmp_path / "chat.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT, attributedBody BLOB,
            handle_id INTEGER, is_from_me INTEGER, is_read INTEGER, item_type INTEGER, associated_message_type INTEGER);
        INSERT INTO handle VALUES (1, '+15195550101'), (2, 'didi@example.com');
        INSERT INTO chat VALUES (1, ''), (2, 'Study group');
        """
    )
    day = datetime(2026, 11, 17, 21, 30, tzinfo=UTC)

    def add(rowid, minutes, text, blob, handle, mine=0, read=0, kind=0, tap=0, chat=1):
        db.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
            (rowid, ns(day.replace(minute=minutes)), text, blob, handle, mine, read, kind, tap),
        )
        db.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, rowid))

    add(1, 1, "are you coming to the tutorial?", None, 1)
    add(2, 5, None, body("sent through the attributed body"), 2, chat=2)
    add(3, 6, "already read", None, 1, read=1)
    add(4, 7, "my own message", None, 1, mine=1)
    add(5, 8, "Loved “hello”", None, 1, tap=2000)
    db.commit()
    db.close()
    return path


def test_only_unread_messages_from_others_are_returned_newest_first(chat_db):
    found = messages.unread(db_path=chat_db)
    assert [m["text"] for m in found] == ["sent through the attributed body", "are you coming to the tutorial?"]
    assert found[0]["from"] == "didi@example.com" and found[0]["group"] == "Study group"


def test_the_summary_names_who_and_when_and_says_when_there_is_nothing(chat_db):
    said = messages.say(messages.unread(db_path=chat_db))
    assert said.startswith("2 unread, newest first:") and "+15195550101" in said and "Study group" in said
    assert messages.say([]) == "No unread messages."


def test_a_long_attributed_body_is_decoded():
    assert messages.decode_body(body("x" * 200)) == "x" * 200
    assert messages.decode_body(None) == ""


def test_a_database_that_cannot_be_opened_says_what_to_switch_on(tmp_path):
    locked = tmp_path / "chat.db"
    locked.write_text("not a database")
    with pytest.raises(macos.ActionError, match="Full Disk Access"):
        messages.unread(db_path=locked)
    with pytest.raises(macos.ActionError, match="no Messages database"):
        messages.unread(db_path=tmp_path / "missing.db")
