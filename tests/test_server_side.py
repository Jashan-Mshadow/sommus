"""Reminders and contacts kept by the always-on brain, and the Mac tools they replace."""

from datetime import datetime

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sommus import brief
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.contacts import ContactBook, find, parse
from sommus.reminders import Reminders

NOW = datetime(2026, 10, 7, 21, 0)


def test_reminders_fire_once_at_their_time_and_survive_a_restart(tmp_path):
    path = tmp_path / "reminders.json"
    book = Reminders(path)
    book.add("Take creatine", "2026-10-07 21:30", NOW)
    book.add("Call the bank", None, NOW)
    assert book.take_due(NOW) == []
    again = Reminders(path)  # the server restarted
    fired = again.take_due(datetime(2026, 10, 7, 21, 31))
    assert [(r.title, late) for r, late in fired] == [("Take creatine", False)]
    assert again.take_due(datetime(2026, 10, 7, 21, 40)) == []  # never twice
    assert [r.title for r in Reminders(path).open()] == ["Call the bank"]


def test_a_missed_reminder_goes_out_late_and_an_old_one_is_dropped(tmp_path):
    book = Reminders(tmp_path / "r.json")
    book.add("Stretch", "2026-10-07 20:00", NOW)
    book.add("Ancient", "2026-10-05 08:00", NOW)
    assert [(r.title, late) for r, late in book.take_due(NOW)] == [("Stretch", True)]


def test_reminders_cancel_by_words_and_show_in_the_brief(tmp_path):
    book = Reminders(tmp_path / "r.json")
    book.add("Take creatine", "2026-10-07 21:30", NOW)
    book.add("Call the bank", None, NOW)
    assert brief.reminders_today(book.open(), NOW) == "Reminders today: Take creatine at 9:30 PM; Call the bank."
    assert [r.title for r in book.cancel("creatine")] == ["Take creatine"]
    assert [r.title for r in book.open()] == ["Call the bank"]


LISTING = """3 contacts:
Mom — phone (647) 625-6520
Didi — phone (647) 832-5827 — email didi@example.com
King Kuljot — email kk@yahoo.com"""


def test_the_macs_contact_list_is_copied_and_searched_the_same_way(tmp_path):
    people = parse(LISTING)
    assert [c.name for c in people] == ["Mom", "Didi", "King Kuljot"]
    assert people[1].phones == ("(647) 832-5827",) and people[1].emails == ("didi@example.com",)
    assert [c.name for c in find(people, "kuljot")] == ["King Kuljot"]
    book = ContactBook(tmp_path / "contacts.txt")
    assert book.replace("Error: Contacts isn't running", NOW) == 0 and book.people == []  # never wiped by an error
    assert book.replace(LISTING, NOW) == 3
    assert [c.name for c in ContactBook(tmp_path / "contacts.txt").people] == ["Mom", "Didi", "King Kuljot"]


async def test_the_servers_contacts_replace_the_macs_and_the_mac_is_still_reachable(tmp_path):
    mac = MCPServer("laptop", log_level="WARNING")

    @mac.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def find_contact(name: str) -> str:
        """Mac contacts."""
        return "from the Mac"

    book = ContactBook(tmp_path / "contacts.txt")
    book.replace(LISTING, NOW)
    async with NodeHub((), Policy()) as hub:
        hub._remote["laptop"] = object()  # stands in for a Mac node reached over the tailnet
        await hub.add("laptop", Client(mac))
        await hub.add("contacts", Client(book.node()))
        result = await hub.call("find_contact", {"name": "didi"})
    assert "832-5827" in result.text


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
SUMMARY:ECE 198 lab
LOCATION:E2 1792
DTSTART;TZID=America/Toronto:20260923T083000
DTEND;TZID=America/Toronto:20260923T112000
RRULE:FREQ=WEEKLY;BYDAY=WE
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Hackathon info session
DTSTART:20261008T230000Z
DTEND:20261009T000000Z
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Reading week
DTSTART;VALUE=DATE:20261012
DTEND;VALUE=DATE:20261017
END:VEVENT
END:VCALENDAR
"""


def test_the_calendar_reads_repeating_and_all_day_events():
    from datetime import date

    from sommus import calendars

    zone = calendars.ZONE
    start, end = datetime(2026, 10, 7, tzinfo=zone), datetime(2026, 10, 13, tzinfo=zone)
    events = calendars.parse(ICS, start, end)
    assert [(e.title, e.start.strftime("%a %H:%M")) for e in events] == [
        ("ECE 198 lab", "Wed 08:30"),  # the weekly lab, expanded
        ("Hackathon info session", "Thu 19:00"),  # UTC turned into Toronto time
        ("Reading week", "Mon 00:00"),
    ]
    now = datetime(2026, 10, 6, 22, 0, tzinfo=zone)
    said = calendars.say(events, date(2026, 10, 7), 1, now)
    assert said == "Tomorrow: 8:30 AM ECE 198 lab (E2 1792)."
    assert "Reading week (all day)" in calendars.say(events, date(2026, 10, 7), 7, now)


def test_calendar_questions_pick_the_days():
    from datetime import date

    from sommus.calendars import asked_range

    wednesday = date(2026, 10, 7)
    assert asked_range(["whats", "on", "my", "calendar"], wednesday) == ("today", 1)
    assert asked_range(["am", "i", "free", "tomorrow"], wednesday) == ("tomorrow", 1)
    assert asked_range(["calendar", "this", "week"], wednesday) == ("today", 7)
    assert asked_range(["schedule", "next", "week"], wednesday) == ("2026-10-12", 7)
    assert asked_range(["plans", "this", "weekend"], wednesday) == ("2026-10-10", 2)
    assert asked_range(["schedule", "for", "monday"], wednesday) == ("2026-10-12", 1)


def test_uw_flow_classes_are_recognised_for_reading_week():
    from sommus.calendars import CLASS_EVENT

    assert CLASS_EVENT.match("ECE150 - LEC 001") and CLASS_EVENT.match("MATH117 - TUT 101")
    assert not CLASS_EVENT.match("Point72 — Info Session") and not CLASS_EVENT.match("Spare")
