"""Fixed commands: what must match, and — more important — what must not."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeModel, build_node, config, text_reply
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sommus.brain import fastpath
from sommus.brain.loop import Brain, TextDelta, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store

WEATHER = lambda tokens, place: "sunny"  # noqa: E731
PLACES = lambda name: fastpath.Place(name.title(), 0.0, 0.0, "Asia/Kolkata")  # noqa: E731


@pytest.mark.parametrize(
    ("text", "intent", "tool", "args", "delta"),
    [
        # people phrase the same request many ways
        ("what's the battery", "battery", "get_battery", {}, 0),
        ("what is my battery at", "battery", "get_battery", {}, 0),
        ("how much battery is left", "battery", "get_battery", {}, 0),
        ("brightness 20", "brightness", "set_brightness", {"level": 20}, 0),
        ("Turn my laptop brightness to 20", "brightness", "set_brightness", {"level": 20}, 0),
        ("lower brightness by 12%", "brightness", "set_brightness", {}, -12),
        ("brightness down 12", "brightness", "set_brightness", {}, -12),
        ("make the screen a bit dimmer", "brightness", "set_brightness", {}, -10),
        ("brightness up", "brightness", "set_brightness", {}, 10),
        ("full brightness", "brightness", "set_brightness", {"level": 100}, 0),
        ("volume 30", "volume", "set_volume", {"level": 30}, 0),
        ("turn the volume up a lot", "volume", "set_volume", {}, 25),
        ("make it slightly louder", "volume", "set_volume", {}, 5),
        ("what's the volume", "volume", "get_volume", {}, 0),
        ("mute", "mute", "set_mute", {"muted": True}, 0),
        ("unmute the sound", "unmute", "set_mute", {"muted": False}, 0),
        ("pause the music", "play_pause", "media_control", {"action": "pause"}, 0),
        ("skip this song", "next", "media_control", {"action": "next"}, 0),
        ("lock my laptop", "lock", "lock_screen", {}, 0),
        ("turn off the screen", "screen_off", "sleep_display", {}, 0),
        ("what time is it", "time", None, {}, 0),
        ("what's the date today", "date", None, {}, 0),
        ("what's the temperature", "weather", None, {}, 0),
        ("is it going to rain tomorrow", "weather", None, {}, 0),
        ("do i need an umbrella", "weather", None, {}, 0),
    ],
)
def test_everyday_requests_skip_the_model(text, intent, tool, args, delta):
    found = fastpath.match(text, WEATHER)
    assert found is not None, text
    assert (found.intent, found.tool, found.args, found.delta) == (intent, tool, args, delta)


@pytest.mark.parametrize(
    "text",
    [
        "set brightness to 20 and open spotify",  # compound
        "why is my battery draining so fast",  # a real question
        "turn off my laptop",  # sleep_computer is not a fast-path action
        "play lofi on spotify",  # needs a search
        "open the screen settings",
        "brightness by 12",  # which direction?
        "brightness 150",
        "what's on my screen",
        "don't mute",
        "lock the door",
        "pause for a second and tell me the time",
        "what's the weather in toronto and open spotify",  # compound, even with a place
        "what time is my class in e7 5353",  # a room, not a place
        "when is my midterm",
        "what time does the store in waterloo close",
        "set a timer in 10 minutes",
    ],
)
def test_anything_less_certain_goes_to_the_model(text):
    assert fastpath.match(text, WEATHER, PLACES) is None


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("63%, discharging, 5:08 remaining, on Battery Power", "You're at 63%, about 5 hours left."),
        ("63%, discharging, 4:41 remaining, on Battery Power", "You're at 63%, about 5 hours left."),
        ("12%, discharging, 0:35 remaining, on Battery Power", "You're at 12%, about 35 minutes left."),
        ("80%, charging, on AC Power", "You're at 80% and charging."),
    ],
)
def test_battery_replies_sound_like_a_person(raw, spoken):
    assert fastpath.say_battery(raw) == spoken


def device_node():
    calls = []
    node = MCPServer("device", log_level="WARNING")

    @node.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False), structured_output=False)
    def set_brightness(level: int) -> str:
        """Set brightness."""
        calls.append(level)
        return f"Brightness {level}%."

    @node.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def get_brightness() -> str:
        """Read brightness."""
        return "Brightness 52%."

    return node, calls


async def test_a_fast_command_never_calls_the_model(tmp_path):
    node, calls = device_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("device", Client(node))
        model = FakeModel()  # no scripted responses: any API call would fail the test
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        events = [e async for e in brain.handle("brightness 20", lambda *_: True)]

        assert calls == [20] and model.requests == []
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Brightness is at 20."]
        assert isinstance(events[-1], TurnDone) and events[-1].cost_usd == 0
        assert brain.messages[-1] == {"role": "assistant", "content": "Brightness is at 20."}  # follow-ups have context
        # The model must see it's done, or it "keeps the promise" on the next request (a reminder made twice).
        assert "set_brightness already ran: Brightness 20%." in brain.messages[-2]["content"]


async def test_a_relative_change_reads_the_level_then_adjusts_it(tmp_path):
    node, calls = device_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("device", Client(node))
        model = FakeModel()
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        [e async for e in brain.handle("lower brightness by 12%", lambda *_: True)]

        assert calls == [40] and model.requests == []  # 52 - 12


async def test_a_failing_local_answer_falls_back_to_the_model(tmp_path):
    node, _ = device_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("device", Client(node))
        model = FakeModel(text_reply("It's sunny."))
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        def broken_weather(tokens, place):
            raise ConnectionError("weather API down")

        brain._weather = broken_weather
        events = [e async for e in brain.handle("what's the temperature", lambda *_: True)]

        assert len(model.requests) == 1
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["It's sunny."]


async def test_a_fast_pattern_for_a_missing_tool_falls_through_to_the_model(tmp_path):
    node, _ = build_node()  # has no set_brightness
    async with NodeHub((), Policy()) as hub:
        await hub.add("test", Client(node))
        model = FakeModel(text_reply("I can't change brightness here."))
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        [e async for e in brain.handle("brightness 20", lambda *_: True)]

        assert len(model.requests) == 1


@pytest.mark.parametrize(
    ("text", "intent", "place"),
    [
        ("what time is it in india", "time_in", "india"),
        ("what's the time in new delhi right now", "time_in", "new delhi"),
        ("time in tokyo", "time_in", "tokyo"),
        ("what is the temperature in waterloo", "weather_in", "waterloo"),
        ("is it going to rain in toronto tomorrow", "weather_in", "toronto"),
        ("what's the weather like in brampton", "weather_in", "brampton"),
    ],
)
def test_time_and_weather_anywhere_skip_the_model(text, intent, place):
    looked_up = []
    found = fastpath.match(
        text, lambda tokens, p: f"{p.name} {tokens}", lambda name: looked_up.append(name) or PLACES(name)
    )
    assert found is not None and found.intent == intent, text
    answer = found.local()
    assert looked_up == [place]
    if intent == "weather_in" and "tomorrow" in text:
        assert "tomorrow" in answer  # the day survives splitting the place off


def test_the_place_is_split_from_the_words_around_it():
    assert fastpath.split_place(fastpath.words("time in new delhi right now")) == (
        ["time", "right", "now"],
        "new delhi",
    )
    assert fastpath.split_place(fastpath.words("volume up")) == (["volume", "up"], None)


EDT = ZoneInfo("America/Toronto")


def test_time_in_a_place_says_how_far_ahead_it_is():
    now = datetime(2026, 9, 15, 14, 18, tzinfo=EDT)
    india = fastpath.Place("India", 22.0, 79.0, "Asia/Kolkata", "IN", True)
    assert fastpath.time_in(india, now) == "It's 11:48 PM in India, 9 and a half hours ahead."
    tokyo = fastpath.Place("Tokyo", 35.7, 139.7, "Asia/Tokyo", "JP")
    assert fastpath.time_in(tokyo, now) == "It's 3:18 AM Wednesday in Tokyo, 13 hours ahead."
    vancouver = fastpath.Place("Vancouver", 49.2, -123.1, "America/Vancouver", "CA")
    assert fastpath.time_in(vancouver, now) == "It's 11:18 AM in Vancouver, 3 hours behind."


def test_a_country_with_many_time_zones_gets_two_cities():
    now = datetime(2026, 9, 15, 14, 18, tzinfo=EDT)
    usa = fastpath.Place("United States", 39.8, -98.5, "America/Chicago", "US", True)
    assert fastpath.time_in(usa, now) == "It's 2:18 PM in New York and 11:18 AM in Los Angeles."


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("when is diwali", "Diwali (Deepavali) is Sunday, November 8."),
        ("when is thanksgiving", "Thanksgiving Day is Monday, October 12."),
        ("when is vaisakhi", "Vaisakhi is Wednesday, April 14, 2027."),
        ("when is family day", "Family Day is Monday, February 15, 2027."),
    ],
)
def test_holiday_dates_are_answered_locally(text, spoken):
    found = fastpath.match(text, WEATHER, PLACES)
    assert found is not None and found.intent == "holiday"
    assert fastpath.when_is(" ".join(fastpath.words(text)[2:]), date(2026, 9, 15)) == spoken


def test_next_holiday():
    assert fastpath.match("what's the next holiday", WEATHER, PLACES).intent == "holiday"
    assert fastpath.next_holidays(date(2026, 9, 15)) == (
        "The next Ontario holiday is Thanksgiving Day, Monday, October 12. "
        "After that, Christmas Day on Friday, December 25."
    )


@pytest.mark.parametrize(
    "text",
    [
        "what are my classes tomorrow",
        "What classes do I have today?",
        "what's on my calendar this week",
        "where is my next class",
        "when is my next lecture",
        "am i free friday afternoon",
        "what's my schedule for monday",
        "do i have any labs this week",
        "what's my schedule looking like tomorrow",  # voice.log, 2026-09-28
    ],
)
def test_calendar_questions_go_to_the_calendar_or_claude_code(text):
    found = fastpath.match(text, WEATHER, PLACES)
    assert found is not None and (found.intent, found.tool) == ("calendar", "calendar_events")
    assert (found.fallback, found.fallback_args) == ("ask_claude", {"task": text})  # no server: Claude Code reads it


@pytest.mark.parametrize(
    "text",
    [
        "add dinner with didi to my calendar friday",  # changes the calendar: the model words the task
        "cancel my classes",
        "what classes should i take next term",
        "when is the next holiday",
        "email my prof that i'll miss class",
    ],
)
def test_calendar_changes_and_other_questions_do_not(text):
    found = fastpath.match(text, WEATHER, PLACES)
    assert found is None or found.intent != "calendar"


APPS = lambda: {"messages": "Messages", "calculator": "Calculator", "chrome": "Google Chrome", "notes": "Notes"}  # noqa: E731


@pytest.mark.parametrize(
    ("text", "intent", "tool", "args", "delta"),
    [
        ("quit messages", "quit_app", "quit_app", {"name": "Messages"}, 0),
        ("close chrome", "quit_app", "quit_app", {"name": "Google Chrome"}, 0),
        ("can you launch the calculator app", "open_app", "open_app", {"name": "Calculator"}, 0),
        ("open up my notes", "open_app", "open_app", {"name": "Notes"}, 0),
        ("what apps do I have open", "list_apps", "list_apps", {}, 0),
        ("which apps are running", "list_apps", "list_apps", {}, 0),
        ("what's on my clipboard", "clipboard", "get_clipboard", {}, 0),
        ("what wifi am I on", "wifi", "get_wifi", {}, 0),
        ("which network am I connected to", "wifi", "get_wifi", {}, 0),
        ("what's playing right now", "now_playing", "get_now_playing", {}, 0),
        ("what song is this", "now_playing", "get_now_playing", {}, 0),
        ("what shortcuts do I have", "shortcuts", "list_shortcuts", {}, 0),
        ("screen's too dim", "brightness", "set_brightness", {}, 10),
        ("the screen is too bright", "brightness", "set_brightness", {}, -10),
        ("it's too loud", "volume", "set_volume", {}, -10),
        ("it's too quiet", "volume", "set_volume", {}, 10),
    ],
)
def test_apps_and_simple_reads(text, intent, tool, args, delta):
    found = fastpath.match(text, apps=APPS)
    assert found is not None and (found.intent, found.tool, found.args, found.delta) == (intent, tool, args, delta)


@pytest.mark.parametrize(
    "text",
    [
        "open spotify",  # not installed: the model can open the web player
        "open my ECE 150 LEARN page",
        "open youtube",
        "open notes and make a list",
        "close everything except chrome and obsidian",
        "copy this to my clipboard",
        "turn off wifi",
        "run my focus shortcut",
        "which apps should I delete",
        "play some music on spotify",
        "is the screen too bright or too dim",
    ],
)
def test_apps_and_reads_leave_the_rest(text):
    found = fastpath.match(text, apps=APPS)
    assert found is None or found.intent not in {
        "open_app", "quit_app", "list_apps", "clipboard", "wifi", "shortcuts", "now_playing", "brightness",
    }  # fmt: skip


@pytest.mark.parametrize(
    "text, follow, intent, delta",
    [
        ("make it brighter", None, "brightness", 10),  # was the volume: the up/down words were shared
        ("dim it", None, "brightness", -10),
        ("brighter please", None, "brightness", 10),
        ("make it louder", None, "volume", 10),
        ("a bit more", "brightness", "brightness", 10),  # follows what was just changed
        ("a bit more", "volume", "volume", 10),
        ("lower", "brightness", "brightness", -10),
        ("turn it up", None, "volume", 10),
        ("turn it down", "brightness", "brightness", -10),
    ],
)
def test_levels_go_to_the_level_that_was_meant(text, follow, intent, delta):
    found = fastpath.match(text, follow=follow)
    assert found and (found.intent, found.delta) == (intent, delta)


@pytest.mark.parametrize("text", ["a bit more", "lower", "higher", "more"])
def test_bare_directions_with_nothing_to_follow_go_to_the_model(text):
    assert fastpath.match(text) is None


@pytest.mark.parametrize(
    "text, action",
    [("pause", "pause"), ("stop", "pause"), ("stop the music", "pause"), ("play", "play"), ("resume", "play")],
)
def test_pause_and_play_are_not_toggles(text, action):
    """A toggle turned 'pause' into play when the music was already paused."""
    found = fastpath.match(text)
    assert found and found.args == {"action": action}


@pytest.mark.parametrize(
    "text",
    [
        "what's on my to-do list",
        "what do I have to do",
        "read my todos",
        "what's next on my to do list",
        "what do I have to do tomorrow",
    ],
)
def test_the_todo_list_is_read_without_the_model(text):
    found = fastpath.match(text, todo=lambda tokens: "Top of your list: gym.")
    assert found and found.intent == "todo" and found.local() == "Top of your list: gym."


@pytest.mark.parametrize("text", ["add milk to my to-do list", "what's on my list", "what do I have to buy"])
def test_todo_changes_and_other_lists_go_to_the_model(text):
    assert fastpath.match(text, todo=lambda tokens: "unused") is None


def test_a_todo_list_for_a_day_passes_the_day_along():
    """voice.log 2026-09-28: 'What is my to-do list for tomorrow?' went to the model: 14 s and 4 cents."""
    found = fastpath.match("What is my to-do list for tomorrow?", todo=lambda tokens: " ".join(tokens))
    assert found and found.intent == "todo" and "tomorrow" in found.local()


def test_tasks_are_read_aloud_briefly():
    assert fastpath.say_tasks([]) == "Your to-do list is clear."
    spoken = fastpath.say_tasks([f"**task {i}**" for i in range(8)])
    assert spoken.startswith("Top of your list: task 0; task 1") and "3 more" in spoken and "*" not in spoken


async def test_a_bit_more_follows_the_level_the_brain_just_changed(tmp_path):
    from sommus.brain.loop import Brain

    node = MCPServer("levels", log_level="WARNING")
    levels = {"brightness": 50, "volume": 50}

    @node.tool(structured_output=False)
    def set_brightness(level: int) -> str:
        """Set brightness."""
        levels["brightness"] = level
        return f"Brightness {level}%."

    @node.tool(structured_output=False)
    def get_brightness() -> str:
        """Read brightness."""
        return f"Brightness {levels['brightness']}%."

    @node.tool(structured_output=False)
    def set_volume(level: int) -> str:
        """Set volume."""
        levels["volume"] = level
        return f"Volume {level}%."

    @node.tool(structured_output=False)
    def get_volume() -> str:
        """Read volume."""
        return f"Volume {levels['volume']}%."

    async with NodeHub((), Policy()) as hub:
        await hub.add("levels", Client(node))
        brain = Brain(config(tmp_path, ask=False), hub, Store(tmp_path / "t.db"), client=FakeModel())
        [e async for e in brain.handle("brightness 40", lambda *_: True)]
        [e async for e in brain.handle("a bit more", lambda *_: True)]
    assert levels == {"brightness": 50, "volume": 50}


# ---------------------------------------------------------------- reminders and timers
AFTERNOON = datetime(2026, 10, 6, 14, 30)


@pytest.mark.parametrize(
    ("said", "title", "due"),
    [
        ("remind me to email warg tonight at 8", "Email warg", "2026-10-06 20:00"),
        ("Remind me to email Warg at 8", "Email Warg", "2026-10-06 20:00"),
        ("remind me tomorrow at 9 to submit the ECE 150 lab", "Submit the ECE 150 lab", "2026-10-07 09:00"),
        ("remind me in 10 minutes to take the pizza out", "Take the pizza out", "2026-10-06 14:40"),
        ("remind me in an hour to stretch", "Stretch", "2026-10-06 15:30"),
        ("Hey Sommus, remind me to call mom tomorrow", "Call mom", "2026-10-07 09:00"),
        ("remind me at 8:30 pm to drink water", "Drink water", "2026-10-06 20:30"),
        ("remind me to drink water at 9am", "Drink water", "2026-10-07 09:00"),
        ("remind me to call mom at 1", "Call mom", "2026-10-07 13:00"),  # 1-6 means the afternoon
        ("remind me to text didi at 5", "Text didi", "2026-10-06 17:00"),
        ("remind me to call mom", "Call mom", None),
    ],
)
def test_reminders_skip_the_model(said, title, due):
    found = fastpath.reminder(said, AFTERNOON)
    assert found and found.tool == "set_reminder" and found.fallback == "create_reminder"
    assert found.args.get("title") == title and found.args.get("due") == due


@pytest.mark.parametrize(
    ("said", "due", "spoken"),
    [
        ("set a timer for 10 minutes", "2026-10-06 14:40", "Timer set for 10 min, it ends at 2:40 PM."),
        ("10 minute timer", "2026-10-06 14:40", None),
        ("timer 25 minutes", "2026-10-06 14:55", None),
        ("set a timer for half an hour", "2026-10-06 15:00", None),
        ("set a timer for 2 hours", "2026-10-06 16:30", "Timer set for 2 hr, it ends at 4:30 PM."),
    ],
)
def test_timers_are_reminders_that_ring_on_time(said, due, spoken):
    found = fastpath.reminder(said, AFTERNOON)
    assert found and found.intent == "timer" and found.args["due"] == due
    if spoken:
        assert found.phrase("") == spoken


@pytest.mark.parametrize(
    "said",
    [
        "remind me what my next class is",  # a question
        "remind me about the meeting",  # no "to": the model decides what to write
        "remind me in a notification to drink water",
        "remind me to email warg and text didi at 8",  # two tasks
        "remind me to buy milk when i get home",  # a condition, not a time
        "remind me to study at 8ish",
        "remind me to stretch on friday at 3",  # a day we don't parse
        "remind me to call mom today at 1",  # already past
        "remind me every day at 8 to take vitamins",
        "set a timer",
    ],
)
def test_reminders_the_template_cant_read_go_to_the_model(said):
    assert fastpath.reminder(said, AFTERNOON) is None


def test_late_at_night_at_8_means_tomorrow_morning():
    found = fastpath.reminder("remind me to email warg at 8", datetime(2026, 10, 6, 23, 0))
    assert found.args["due"] == "2026-10-07 08:00" and found.phrase("") == "Done, I'll remind you tomorrow at 8 AM."


@pytest.mark.parametrize(
    ("said", "on"),
    [
        ("turn on my flashlight", True),
        ("flashlight on", True),
        ("turn the torch off", False),
        ("kill the flashlight", False),
    ],
)
def test_the_flashlight_is_the_phones(said, on):
    found = fastpath.match(said)
    assert found and found.tool == "phone_flashlight" and found.args == {"on": on}


@pytest.mark.parametrize("said", ["is my flashlight on", "turn on the lights in my room", "flashlight"])
def test_flashlight_questions_go_to_the_model(said):
    found = fastpath.match(said)
    assert not found or found.tool != "phone_flashlight"
