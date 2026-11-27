"""The morning brief, the evening heads-up, the class nudges and the phone's queued actions."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sommus import brief
from sommus.brain import campus

ZONE = ZoneInfo("America/Toronto")
SCHEDULE = """
timezone = "America/Toronto"
term_start = 2026-09-08
term_end = 2026-12-08
weekly = [
  { course = "ECE 105", kind = "LEC", day = "Tue", start = 08:30:00, end = 09:20:00, room = "E2 1792" },
]
deadlines = [
  { what = "Python assignment", due = 2026-10-09T23:59:00 },
]
"""


def schedule(tmp_path):
    path = tmp_path / "schedule.toml"
    path.write_text(SCHEDULE)
    return campus.Schedule.load(path)


def test_a_class_is_nudged_once_inside_the_window(tmp_path):
    s, sent = schedule(tmp_path), set()
    early = datetime(2026, 10, 6, 8, 10, tzinfo=ZONE)
    assert brief.class_nudges(s, early, 15, sent) == []  # 20 minutes out
    now = datetime(2026, 10, 6, 8, 16, tzinfo=ZONE)
    assert brief.class_nudges(s, now, 15, sent) == ["ECE 105 lecture in 14 minutes, in E2 1792."]
    assert brief.class_nudges(s, now, 15, sent) == []  # not twice


def test_the_morning_brief_and_the_evening_heads_up(tmp_path):
    s = schedule(tmp_path)
    morning = datetime(2026, 10, 8, 7, 0, tzinfo=ZONE)
    text = brief.morning(s, morning, "4° and clear.", "2 new emails: Davison (groups); Learn (grades).", ["**gym**"])
    assert text.startswith("Morning. Thursday, October 8.")
    assert "Python assignment" in text and "4° and clear." in text and "Top of your list: gym." in text
    evening = datetime(2026, 10, 8, 20, 0, tzinfo=ZONE)
    assert brief.due_tomorrow(s, evening) == "Heads up, due tomorrow: Python assignment."
    assert brief.due_tomorrow(s, datetime(2026, 10, 6, 20, 0, tzinfo=ZONE)) == ""


def test_unread_mail_is_summarised_by_sender_and_subject():
    listing = (
        "2 messages for 'is:unread' (• = unread):\n"
        '[57604] • "Davison" <d@uwaterloo.ca> — ECE 190 groups (Tue, 06 Oct 2026 22:35:08 +0000)\n'
        "[57603] • Learn <noreply@uwaterloo.ca> — Grades posted (Tue, 6 Oct 2026 20:41:54 +0000)"
    )
    assert brief.unread_summary(listing) == "2 new emails: Davison (ECE 190 groups); Learn (Grades posted)."
    assert brief.unread_summary("No messages match 'is:unread'.") == ""


async def test_phone_actions_queue_only_for_the_iphone():
    from mcp import Client

    from sommus.server import Phone

    phone = Phone()
    async with Client(phone.node()) as client:
        away = await client.call_tool("phone_flashlight", {"on": True})
        assert "only works" in away.content[0].text and phone.action == {}
        phone.present = True
        await client.call_tool("phone_flashlight", {"on": True})
        second = await client.call_tool("phone_alarm", {"time_24h": "07:30"})
    assert phone.action == {"action": "flashlight_on"} and "one thing per request" in second.content[0].text


async def test_the_phone_can_send_long_text_to_telegram():
    from mcp import Client

    from sommus.server import Phone

    sent: list[str] = []

    async def tell(text: str, buttons=None) -> None:
        sent.append(text)

    phone = Phone()
    async with Client(phone.node()) as client:
        before = await client.call_tool("send_telegram_message", {"text": "hi"})
        phone.telegram = tell
        blank = await client.call_tool("send_telegram_message", {"text": "  "})
        done = await client.call_tool("send_telegram_message", {"text": "Upper day: chest fly, lat pulldown"})
    assert "isn't connected" in before.content[0].text and "Nothing to send" in blank.content[0].text
    assert sent == ["Upper day: chest fly, lat pulldown"] and "Sent" in done.content[0].text


def test_the_calendar_does_not_read_out_what_already_happened_today():
    from datetime import date, datetime

    from sommus import calendars

    zone = calendars.ZONE
    morning = calendars.Event(
        "ECE 190", datetime(2026, 11, 4, 11, 30, tzinfo=zone), datetime(2026, 11, 4, 12, 20, tzinfo=zone), False
    )
    evening = calendars.Event(
        "Study group", datetime(2026, 11, 4, 22, 0, tzinfo=zone), datetime(2026, 11, 4, 23, 0, tzinfo=zone), False
    )
    day = date(2026, 11, 4)
    night = datetime(2026, 11, 4, 21, 43, tzinfo=zone)
    assert calendars.say([morning], day, 1, night) == "Today: nothing left (1 already finished)."
    said = calendars.say([morning, evening], day, 1, night)
    assert "ECE 190" not in said and "Study group" in said
    assert calendars.say([], day, 1, night) == "Today: nothing on your calendar."


def test_the_second_block_of_a_double_lecture_is_not_nudged_again(tmp_path):
    path = tmp_path / "double.toml"
    extra = [
        ("ECE 105", "09:30:00", "10:20:00", "E2 1792"),
        ("MATH 115", "13:30:00", "14:20:00", "E7 5353"),
        ("MATH 115", "15:30:00", "16:20:00", "E7 5353"),
    ]
    rows = "".join(
        f'  {{ course = "{c}", kind = "LEC", day = "Tue", start = {a}, end = {b}, room = "{r}" }},\n'
        for c, a, b, r in extra
    )
    path.write_text(SCHEDULE.replace("weekly = [\n", "weekly = [\n" + rows))
    s, sent = campus.Schedule.load(path), set()
    # ECE 105 at 8:30 and 9:30 are back to back: the 9:15 nudge for the second block is skipped
    first = datetime(2026, 10, 6, 8, 16, tzinfo=ZONE)
    assert brief.class_nudges(s, first, 15, sent) == ["ECE 105 lecture in 14 minutes, in E2 1792."]
    second = datetime(2026, 10, 6, 9, 16, tzinfo=ZONE)
    assert brief.class_nudges(s, second, 15, sent) == []
    # MATH 115 at 13:30 and 15:30 have a gap: both are nudged
    assert len(brief.class_nudges(s, datetime(2026, 10, 6, 13, 16, tzinfo=ZONE), 15, sent)) == 1
    assert len(brief.class_nudges(s, datetime(2026, 10, 6, 15, 16, tzinfo=ZONE), 15, sent)) == 1
