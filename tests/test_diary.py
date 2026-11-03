"""The diary: days distilled overnight from the turn log, recalled into the prompt without breaking its cache."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from fakes import FakeModel, config, text_reply

from sommus.brain import diary, memory
from sommus.brain.diary import Entry, FollowUp
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store

TODAY = date(2026, 11, 5)  # a Thursday


def sample(path):
    diary.save(
        path,
        [
            Entry(date(2026, 10, 12), "Jashan prepared for his Amazon interview and was nervous about it."),
            Entry(date(2026, 10, 20), "He played basketball with Arjun and skipped the physics tutorial."),
            Entry(date(2026, 11, 2), "He studied for the ECE 105 midterm until 2 AM."),
            Entry(
                date(2026, 11, 3),
                "He finished the linear algebra assignment.",
                [FollowUp("Ask how the ECE 105 midterm went", date(2026, 11, 5)), FollowUp("Ask if he called home")],
            ),
            Entry(date(2026, 11, 4), "He went to the gym and cooked dal."),
        ],
    )


def test_the_file_reads_back_what_was_written_and_a_rerun_replaces_the_day(tmp_path):
    path = tmp_path / "Diary.md"
    sample(path)
    known = diary.entries(path)
    assert [e.day for e in known][-1] == date(2026, 11, 4)
    assert known[3].follow_ups[0] == FollowUp("Ask how the ECE 105 midterm went", date(2026, 11, 5))
    assert "## 2026-11-03 (Tuesday)" in path.read_text()

    diary.put(path, Entry(date(2026, 11, 4), "He went to the gym."))
    assert [e.summary for e in diary.entries(path) if e.day == date(2026, 11, 4)] == ["He went to the gym."]
    assert len(diary.entries(path)) == 5


def test_hand_edits_survive_parsing(tmp_path):
    path = tmp_path / "Diary.md"
    path.write_text("# Sommus Diary\n\nnotes\n\n## 2026-11-01\nLine one.\nLine two.\n- Follow up: Ask about it\n")
    [entry] = diary.entries(path)
    assert entry.summary == "Line one. Line two." and entry.follow_ups == [FollowUp("Ask about it")]


def test_the_prompt_shows_the_last_few_days_and_only_follow_ups_due_now(tmp_path):
    path = tmp_path / "Diary.md"
    sample(path)
    block = diary.prompt_block(path, "Jashan", TODAY)
    assert "Yesterday (Wed Nov 4): He went to the gym and cooked dal." in block
    assert "Tuesday (Tue Nov 3)" in block and "Monday (Mon Nov 2)" in block
    assert "Amazon" not in block and "basketball" not in block  # older days come back only by recall
    assert "- Ask how the ECE 105 midterm went" in block and "- Ask if he called home" in block

    early = diary.prompt_block(path, "Jashan", date(2026, 11, 4))
    assert "midterm went" not in early  # dated for the 5th: not before
    late = diary.prompt_block(path, "Jashan", date(2026, 11, 12))
    assert "midterm went" not in late and "called home" not in late  # stale follow-ups drop out
    assert diary.prompt_block(tmp_path / "none.md", "Jashan", TODAY) == ""


def test_an_older_day_is_recalled_only_when_the_request_is_about_it(tmp_path):
    path = tmp_path / "Diary.md"
    sample(path)
    note = diary.recall_note(path, "did I ever hear back about the interview?", TODAY)
    assert "Mon Oct 12" in note and "Amazon interview" in note
    assert "Oct 20" in diary.recall_note(path, "when did I last play basketball with Arjun", TODAY)
    # Common words, or days already in the prompt, bring nothing.
    assert diary.recall_note(path, "what's the weather today", TODAY) == ""
    assert diary.recall_note(path, "turn the volume up", TODAY) == ""
    assert diary.matches(path, "how did the midterm go", TODAY) == []  # Nov 2/3 are in the prompt already


def test_search_by_day_or_words(tmp_path):
    path = tmp_path / "Diary.md"
    sample(path)
    assert "gym" in diary.search(path, "yesterday", TODAY)
    assert "linear algebra" in diary.search(path, "tuesday", TODAY)
    assert "Amazon" in diary.search(path, "2026-10-12", TODAY)
    assert "basketball" in diary.search(path, "Arjun", TODAY)
    assert "Nothing in the diary mentions" in diary.search(path, "skydiving", TODAY)
    assert "empty" in diary.search(tmp_path / "none.md", "", TODAY)


def test_forget_removes_sentences_follow_ups_or_a_whole_day(tmp_path):
    path = tmp_path / "Diary.md"
    sample(path)
    assert diary.forget(path, "called home") == "Forgot from the diary: Ask if he called home."
    assert diary.forget(path, "dal") == "Forgot from the diary: He went to the gym and cooked dal."
    assert date(2026, 11, 4) not in {e.day for e in diary.entries(path)}  # nothing left of that day
    assert diary.forget(path, "2026-10-12") == "Forgot the diary entry for Monday October 12."
    assert date(2026, 10, 12) not in {e.day for e in diary.entries(path)}
    with pytest.raises(memory.MemoryError, match="Nothing remembered"):
        diary.forget(path, "skydiving")
    with pytest.raises(memory.MemoryError, match="more specific"):
        diary.forget(path, "he")


def test_the_transcript_keeps_his_words_and_drops_pins_and_raw_tool_output():
    rows = [
        ("2026-11-04T09:00:00", "[PIN entered]", "", "pin"),
        ("2026-11-04T09:01:00", "what's on today", "Physics at 10.", "fastpath"),
        ("2026-11-04T09:01:30", "what's on today", "Physics at 10.", "fastpath"),
        ("2026-11-04T10:00:00", "[gemini-live] text Arjun", "{raw tool output}", "gemini-live"),
    ]
    text = diary.transcript(rows)
    assert "PIN" not in text and text.count("what's on today") == 1
    assert "09:01 Him: what's on today\n      You: Physics at 10." in text
    assert "Him: text Arjun" in text and "raw tool output" not in text


def reply_json(**found):
    return text_reply(json.dumps(found))


def logged(store, *turns):
    for said, reply in turns:
        turn = store.start_turn(said)
        store._db.execute("UPDATE turns SET reply=?, status='ok', model='claude-sonnet-5' WHERE id=?", (reply, turn))
    store._db.commit()


async def test_the_night_writes_the_day_saves_new_facts_and_closes_settled_follow_ups(tmp_path):
    store = Store(tmp_path / "t.db")
    path, facts = tmp_path / "Diary.md", tmp_path / "Memory.md"
    diary.save(path, [Entry(date(2026, 11, 3), "Studied.", [FollowUp("Ask if he called home")])])
    logged(store, ("I have my physics midterm thursday", "Good luck."), ("called mom, all good", "Nice."))
    model = FakeModel(
        reply_json(
            summary="He has the physics midterm on Thursday. He called his mom. His PIN is 4321.",
            follow_ups=[{"text": "Ask how the physics midterm went", "on": "2026-11-06"}],
            facts=["Calls his mom on Wednesdays", "His password is hunter2"],
            done=["Ask if he called home"],
        )
    )
    written = await diary.write_day(
        store, model, "claude-haiku-4-5", path, facts, date(2026, 11, 4), "2026-01-01", "2099-01-01"
    )

    request = model.requests[0]
    assert request["model"] == "claude-haiku-4-5" and "Ask if he called home" in request["system"]
    assert "Him: I have my physics midterm thursday" in request["messages"][0]["content"]
    entry = diary.entries(path)[-1]
    assert entry.day == date(2026, 11, 4) and "PIN" not in entry.summary and "called his mom" in entry.summary
    assert entry.follow_ups == [FollowUp("Ask how the physics midterm went", date(2026, 11, 6))]
    assert diary.entries(path)[0].follow_ups == [] and written.closed == ["Ask if he called home"]
    assert memory.facts(facts) == ["Calls his mom on Wednesdays"]  # the secret one never lands
    assert written.cost_usd > 0
    # The night's own call is logged, so the month's budget counts it, and it isn't summarised next night.
    assert store.cost_month() > 0
    assert "[diary]" not in diary.transcript(store.turns_between("2026-01-01", "2099-01-01"))


async def test_a_quiet_day_costs_nothing(tmp_path):
    model = FakeModel()
    written = await diary.write_day(
        Store(tmp_path / "t.db"), model, "m", tmp_path / "D.md", None, TODAY, "2026-01-01", "2099-01-01"
    )
    assert written.entry is None and model.requests == []


async def test_the_brain_keeps_one_system_prompt_and_puts_recall_on_the_request(tmp_path):
    from sommus.brain.loop import Brain

    path = tmp_path / "Diary.md"
    sample(path)
    cfg = replace(config(tmp_path), diary_path=path, memory_path=tmp_path / "Memory.md")
    async with NodeHub((), Policy({})) as hub:
        model = FakeModel(text_reply("Hi."), text_reply("Not yet."))
        brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=model)
        assert "Your diary" in brain.system and "bring up one thing" in brain.system
        [e async for e in brain.handle("hello", None)]
        [e async for e in brain.handle("any news on the amazon interview?", None)]
    first, second = model.requests
    assert first["system"] == second["system"]  # the cached prefix is untouched by recall
    asked = second["messages"][-1]["content"][0]["text"]
    assert "any news on the amazon interview?" in asked and "[From your diary" in asked and "Oct 12" in asked
    assert "[From your diary" not in first["messages"][-1]["content"][0]["text"]


async def test_the_server_writes_the_diary_once_a_night(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from sommus.server import Phone, Server

    cfg = replace(config(tmp_path), diary_path=tmp_path / "Diary.md", memory_path=tmp_path / "Memory.md")
    store = Store(tmp_path / "t.db")
    logged(store, ("I start my co-op search on monday", "Good luck."))
    async with NodeHub((), Policy({})) as hub:
        server = Server(cfg, hub, store, Phone(), log=lambda line: None)
        model = FakeModel(reply_json(summary="He planned to start his co-op search on Monday.", facts=[]))
        server.brain.client = model
        zone = datetime.now().astimezone().tzinfo
        clock = datetime.now() + timedelta(minutes=1)  # the turn log's clock: the turn above is in the last day
        await server.write_diary(datetime(2026, 11, 5, 2, 0, tzinfo=zone), clock)  # before 03:30: nothing
        assert model.requests == []
        await server.write_diary(datetime(2026, 11, 5, 3, 31, tzinfo=zone), clock)
        await server.write_diary(datetime(2026, 11, 5, 4, 0, tzinfo=zone), clock)  # not twice
    assert len(model.requests) == 1
    [entry] = diary.entries(cfg.diary_path)
    assert entry.day == date(2026, 11, 4) and "co-op" in entry.summary
    assert server.state["diary_upto"] == clock.isoformat(timespec="seconds")


def test_day_for():
    assert diary.day_for(datetime(2026, 11, 5, 3, 30), "03:30") == date(2026, 11, 4)
    assert diary.day_for(datetime(2026, 11, 5, 23, 50), "23:50") == date(2026, 11, 5)
