"""Campus engine: class and deadline answers from a schedule file, with no model."""

import tomllib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from sommus.brain import campus, fastpath

ZONE = ZoneInfo("America/Toronto")
SCHEDULE = campus.Schedule(
    tomllib.loads("""
term_start = 2026-09-08
term_end = 2026-12-08
breaks = [{ from = 2026-10-12, to = 2026-10-16 }]
weekly = [
  { course = "ECE 150", kind = "LEC", day = "Mon", start = 08:30:00, end = 09:20:00, room = "E7 5353" },
  { course = "MATH 117", kind = "LEC", day = "Mon", start = 11:30:00, end = 12:20:00, room = "E7 5353" },
  { course = "MATH 117", kind = "TUT", day = "Mon", start = 14:30:00, end = 16:20:00, room = "DWE 3517" },
  { course = "MATH 115", kind = "TUT", day = "Tue", start = 14:30:00, end = 16:20:00, room = "DWE 3516", first = 2026-09-22, skip = [{ from = 2026-10-21, to = 2026-10-27 }] },
]
once = [
  { course = "ECE 198", kind = "LAB", date = 2026-09-23, start = 08:30:00, end = 10:20:00, room = "E2 1792" },
  { course = "MATH 115", kind = "EXAM", date = 2026-10-26, start = 20:00:00, room = "TBA" },
]
deadlines = [
  { what = "ECE 150 Project 1", due = 2026-09-29T22:00:00 },
  { what = "ECE 105 Assignment 1", due = 2026-09-20T23:59:00 },
]
""")
)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ZONE)


def test_weekly_slots_respect_first_date_skips_and_breaks():
    assert [s.course for s in SCHEDULE.on(date(2026, 9, 15))] == []  # MATH 115 TUT starts Sept 22
    assert [s.course for s in SCHEDULE.on(date(2026, 9, 22))] == ["MATH 115"]
    assert SCHEDULE.on(date(2026, 10, 13)) == []  # reading week
    assert SCHEDULE.on(date(2026, 10, 27)) == []  # skipped around the midterm
    assert [s.kind for s in SCHEDULE.on(date(2026, 10, 26))] == ["LEC", "LEC", "TUT", "EXAM"]
    assert SCHEDULE.on(date(2026, 12, 14)) == []  # after term


def test_next_class_later_today_with_countdown():
    assert campus.next_class(SCHEDULE, at("2026-09-21 07:50")) == (
        "Next is ECE 150 lecture at 8:30 AM in E7 5353, in 40 minutes."
    )


def test_next_class_mentions_the_one_you_are_in():
    out = campus.next_class(SCHEDULE, at("2026-09-21 11:45"))
    assert out.startswith("You're in MATH 117 lecture until 12:20 PM. Next is MATH 117 tutorial at 2:30 PM")


def test_next_class_skips_the_weekend_and_reading_week():
    assert "Monday at 8:30 AM: ECE 150 lecture" in campus.next_class(SCHEDULE, at("2026-09-19 15:00"))
    assert "Monday, October 19" in campus.next_class(SCHEDULE, at("2026-10-09 17:00"))


def test_today_lists_only_what_is_left():
    out = campus.day_summary(SCHEDULE, date(2026, 9, 21), at("2026-09-21 10:00"))
    assert out == "2 more today: 11:30 AM MATH 117 lecture in E7 5353; 2:30 PM MATH 117 tutorial in DWE 3517."
    done = campus.day_summary(SCHEDULE, date(2026, 9, 21), at("2026-09-21 17:00"))
    assert done.startswith("You're done for today.") and "tomorrow at 2:30 PM" in done


def test_other_day_has_finish_time_and_hides_tba_rooms():
    out = campus.day_summary(SCHEDULE, date(2026, 10, 26), at("2026-10-25 12:00"))
    assert out.startswith("Tomorrow: 8:30 AM ECE 150 lecture")
    assert "8 PM MATH 115 midterm." in out and "TBA" not in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("where's my next class", "Next is MATH 117 lecture at 11:30 AM"),
        ("when is my next lecture", "Next is MATH 117 lecture"),
        ("what classes do I have tomorrow", "Tomorrow: 2:30 PM MATH 115 tutorial"),
        ("any more classes today", "2 more today"),
        ("what's my first class on monday", "Your first class today is ECE 150 lecture at 8:30 AM"),
        ("classes on tuesday", "Tomorrow:"),
    ],
)
def test_answers_by_question(text, expected):
    assert expected in campus.answer(SCHEDULE, fastpath.words(text), at("2026-09-21 09:40"))


def test_due_this_week_and_on_a_day():
    now = at("2026-09-19 12:00")
    assert campus.due_summary(SCHEDULE, now) == "Due soon: ECE 105 Assignment 1, tomorrow."
    assert campus.due_answer(SCHEDULE, fastpath.words("what's due tomorrow"), now) == (
        "Due tomorrow: ECE 105 Assignment 1."
    )
    assert campus.due_answer(SCHEDULE, fastpath.words("anything due today"), now) == "Nothing due today."
    later = at("2026-09-27 09:00")
    assert "ECE 150 Project 1, Tuesday at 10 PM" in campus.due_answer(SCHEDULE, ["due"], later)


def test_today_json_shape(tmp_path):
    todo = tmp_path / "TODO.md"
    todo.write_text("# TODO\n\n## Weekend\n- [ ] 1. ECE 190 video [[Notes|here]]\n- [x] done\n\n## Later\n- [ ] no\n")
    data = campus.write_today(SCHEDULE, at("2026-09-21 07:00"), tmp_path / "today.json", todo)
    assert (tmp_path / "today.json").exists()
    assert data["date"] == "2026-09-21" and len(data["classes_today"]) == 3
    assert data["next_class"]["course"] == "ECE 150" and data["next_class"]["kind"] == "lecture"
    assert data["open_tasks"] == ["ECE 190 video Notes"]


CAMPUS = lambda kind, tokens: f"{kind}:{' '.join(tokens)}"  # noqa: E731


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("where's my next class", "campus"),
        ("What classes do I have today?", "campus"),
        ("what are my classes tomorrow", "campus"),
        ("when does my first lecture start tomorrow", "campus"),
        ("do I have any labs on wednesday", "campus"),
        ("what's due this week", "due"),
        ("anything due tomorrow", "due"),
        ("deadlines", "due"),
    ],
)
def test_fast_path_claims_class_and_deadline_questions(text, intent):
    found = fastpath.match(text, campus=CAMPUS)
    assert found is not None and found.intent == intent and found.local is not None


@pytest.mark.parametrize(
    "text",
    [
        "what's on my calendar this week",  # personal calendar: Claude Code reads Google
        "am I free tomorrow afternoon",
        "cancel my classes",
        "what classes should i take next term",
        "when is my math 115 midterm",
        "email my prof that the assignment is due tomorrow",
        "why is the deadline so early",
    ],
)
def test_fast_path_leaves_everything_else(text):
    found = fastpath.match(text, campus=CAMPUS)
    assert found is None or found.intent not in {"campus", "due"}
