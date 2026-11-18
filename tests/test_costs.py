"""'How much has this cost?': the window is understood and the answer comes from the turn log."""

from datetime import datetime, timedelta

import pytest

from sommus import costs
from sommus.brain.store import Store, Usage

NOW = datetime(2026, 11, 17, 22, 30)


@pytest.mark.parametrize(
    ("said", "back", "label"),
    [
        ("how much has sommus cost in the past hour", timedelta(hours=1), "the past hour"),
        ("how much did this cost in the last 10 minutes", timedelta(minutes=10), "the past 10 minutes"),
        ("what has it cost me in the past ten days", timedelta(days=10), "the past 10 days"),
        ("how much have I spent over the last two weeks", timedelta(weeks=2), "the past 2 weeks"),
    ],
)
def test_the_window_is_read_from_the_question(said, back, label):
    since, window = costs.parse(said, NOW)
    assert since == NOW - back and window == label


def test_today_week_and_month_have_their_own_starts():
    assert costs.parse("how much has sommus cost today", NOW) == (datetime(2026, 11, 17), "today")
    assert costs.parse("how much did it cost this week", NOW)[0] == datetime(2026, 11, 16)
    assert costs.parse("what is the bill this month", NOW)[0] == datetime(2026, 11, 1)
    assert costs.parse("how much has it cost", NOW)[1] == "today"


@pytest.mark.parametrize(
    "said", ["what is the weather", "cost of living in Waterloo", "play some music", "I spent an hour studying"]
)
def test_other_questions_are_not_claimed(said):
    assert costs.parse(said, NOW) is None


def test_money_is_said_the_way_a_person_would():
    assert costs.say_money(0) == "nothing" and costs.say_money(0.004) == "under a cent"
    assert costs.say_money(0.0412) == "4.1 cents" and costs.say_money(2.5) == "$2.50"
    assert costs.say("the past hour", 0, 0, 0) == "No requests in the past hour, so nothing spent."
    assert costs.say("today", 6, 2, 0.12) == "In today: 12.0 cents across 6 requests, 2 of them free on the fast path."


def test_the_turn_log_is_summed_for_a_window_and_pin_entries_are_left_out(tmp_path):
    store = Store(tmp_path / "t.db")
    for text, model in [("a", "fastpath"), ("b", "claude-sonnet-5"), ("[PIN entered]", "pin")]:
        store.finish_turn(store.start_turn(text), "ok", "ok", model, Usage(output_tokens=1000))
    count, fast, dollars = store.cost_since(datetime.now() - timedelta(minutes=5))
    assert (count, fast) == (2, 1) and dollars > 0
    assert store.cost_since(datetime.now() + timedelta(minutes=5)) == (0, 0, 0)
