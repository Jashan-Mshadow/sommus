"""Wake-ups: "wake me up at 7:50" kept on the server, said on the Mac at the time (loud enough, even in quiet
hours, never in a class), sent to Telegram, repeated until he answers. Nothing is spoken here: the Mac is fake."""

from datetime import datetime

import pytest
from fakes import FakeModel, config, text_reply
from mcp import Client

from sommus import activity, wakeup
from sommus.brain import fastpath
from sommus.brain.nodes import ToolOutput
from sommus.reminders import Reminders

LATE_NIGHT = datetime(2026, 11, 26, 23, 10, 20)  # Thursday, 11:10 PM
DUE = datetime(2026, 11, 27, 7, 50)


# ---------------------------------------------------------------- reading the request ($0)


@pytest.mark.parametrize(
    ("text", "due", "say", "once", "said"),
    [
        ("wake me up at 7:50", "2026-11-27 07:50", "", False, "Wake-up set for 7:50 AM tomorrow."),
        ("Wake me up at 7:50 AM", "2026-11-27 07:50", "", False, "Wake-up set for 7:50 AM tomorrow."),
        ("wake me up in 20 minutes", "2026-11-26 23:30", "", False, "Wake-up set for 11:30 PM, in 20 minutes."),
        ("wake me up tomorrow at 7", "2026-11-27 07:00", "", False, "Wake-up set for 7 AM tomorrow."),
        ("wake me at 7 tomorrow morning", "2026-11-27 07:00", "", False, "Wake-up set for 7 AM tomorrow."),
        ("wake me up tomorrow at 5", "2026-11-27 05:00", "", False, "Wake-up set for 5 AM tomorrow."),  # not 5 PM
        ("wake me up tomorrow at 12", "2026-11-27 12:00", "", False, "Wake-up set for 12 PM tomorrow."),
        ("hey sommus can you wake me up at 7:50 please", "2026-11-27 07:50", "", False,
         "Wake-up set for 7:50 AM tomorrow."),
        ("set a spoken alarm for 8", "2026-11-27 08:00", "", False, "Wake-up set for 8 AM tomorrow."),
        ("set a wake-up alarm for 6:45 am", "2026-11-27 06:45", "", False, "Wake-up set for 6:45 AM tomorrow."),
        ("set a wake up call for 7", "2026-11-27 07:00", "", False, "Wake-up set for 7 AM tomorrow."),
        ("wake me up at 7 for the gym", "2026-11-27 07:00", "Time for the gym.", False,
         "Wake-up set for 7 AM tomorrow."),
        ("wake me up at 7 to go to the gym", "2026-11-27 07:00", "Time to go to the gym.", False,
         "Wake-up set for 7 AM tomorrow."),
        ("at 7:50 say time for the gym out loud", "2026-11-27 07:50", "Time for the gym", True,
         'I\'ll say "Time for the gym" out loud at 7:50 AM tomorrow.'),
        ("say time for the gym out loud at 7:50", "2026-11-27 07:50", "Time for the gym", True,
         'I\'ll say "Time for the gym" out loud at 7:50 AM tomorrow.'),
        ("in 10 minutes say dinner is ready out loud", "2026-11-26 23:20", "Dinner is ready", True,
         'I\'ll say "Dinner is ready" out loud at 11:20 PM.'),
        ("announce dinner is ready in 10 minutes", "2026-11-26 23:20", "Dinner is ready", True,
         'I\'ll say "Dinner is ready" out loud at 11:20 PM.'),
    ],
)  # fmt: skip
def test_the_common_ways_of_asking_are_free(text, due, say, once, said):
    found = wakeup.match(text, LATE_NIGHT)
    assert found is not None and found.tool == "schedule_wakeup", text
    assert found.args == {"due": due, "say": say, "once": once}
    assert found.phrase("Wake-up set for ...") == said


def test_no_am_pm_means_whichever_comes_next_like_the_plug_timers():
    afternoon = datetime(2026, 11, 26, 14, 0)
    assert wakeup.match("wake me up at 4", afternoon).args["due"] == "2026-11-26 16:00"  # a nap
    assert wakeup.match("wake me up at 1:30", afternoon).args["due"] == "2026-11-27 01:30"
    assert wakeup.match("wake me up at 19:30", afternoon).args["due"] == "2026-11-26 19:30"
    after_midnight = datetime(2026, 11, 27, 0, 30)
    assert wakeup.match("wake me up at 7:50", after_midnight).args["due"] == "2026-11-27 07:50"
    assert wakeup.match("wake me up tonight at 11", afternoon).args["due"] == "2026-11-26 23:00"


@pytest.mark.parametrize(
    "text",
    [
        "wake me up when class ends",  # no time
        "wake me up tonight at 11",  # 11 PM tonight is gone at 11:10 PM: the model asks, nothing guesses
        "remind me to wake up at 7",  # a reminder, not a wake-up
        "wake me up at 7 and turn on the lamp",  # two things
        "wake me up at 7 for the gym and lamp on",
        "at 7 say hi",  # "say" without "out loud" could mean anything
        "say hello out loud",  # now: that's announce, not later
        "at 7 say it out loud",  # say what?
        "set an alarm for 7",  # a phone alarm (or the model's choice), not claimed here
        "did my alarm go off",
        "what time should i wake up",
        "wake me up at 25:00",
        "wake me up in 3 days",
        "cancel my reminder",
        "how do I cancel an alarm on my phone",
    ],
)
def test_what_is_not_a_plain_wake_up_goes_to_the_model(text):
    assert wakeup.match(text, LATE_NIGHT) is None, text


@pytest.mark.parametrize(
    "text", ["cancel my alarm", "cancel the wake-up", "cancel my wake up call", "turn off my alarms",
             "delete all my alarms", "cancel my alarm for tomorrow", "remove the wake-up please"],
)  # fmt: skip
def test_cancelling_is_free(text):
    found = wakeup.match(text, LATE_NIGHT)
    assert found is not None and found.tool == "cancel_wakeup", text


def test_the_wake_up_phrases_do_not_change_what_the_rest_of_the_fast_path_claims():
    assert fastpath.match("remind me to wake up at 7").tool == "set_reminder"
    assert fastpath.match("turn off the lamp in 5 minutes", now=LATE_NIGHT).tool == "schedule_plug"


@pytest.mark.parametrize("text", ["I'm up", "im up", "okay", "ok I'm awake", "stop", "turn off the alarm",
                                  "thanks sommus", "alright alright I'm getting up"])  # fmt: skip
def test_these_say_he_is_awake(text):
    assert wakeup.is_ack(text), text


@pytest.mark.parametrize("text", ["what's the weather", "wake me up in 10 minutes", "turn on the lamp", ""])
def test_these_are_requests_not_just_an_answer(text):
    assert not wakeup.is_ack(text), text


# ---------------------------------------------------------------- setting, listing, cancelling


async def test_set_list_and_cancel_with_the_reminders(tmp_path):
    reminders = Reminders(tmp_path / "r.json")
    wakes = wakeup.WakeUps(reminders, FakeMac(), tmp_path, clock=lambda: LATE_NIGHT)
    async with Client(wakes.node()) as client, Client(reminders.node(clock=lambda: LATE_NIGHT)) as rem:
        said = (await client.call_tool("schedule_wakeup", {"due": "2026-11-27 07:50"})).content[0].text
        assert said.startswith("Wake-up set for 2026-11-27 07:50")
        await client.call_tool("schedule_wakeup", {"due": "2026-11-27 08:30", "say": "Gym", "once": True})
        listed = (await rem.call_tool("upcoming_reminders", {})).content[0].text
        assert "Wake-up (2026-11-27 07:50" in listed and "Say out loud: Gym (2026-11-27 08:30" in listed
        gone = (await client.call_tool("schedule_wakeup", {"due": "2026-11-26 22:00"})).content[0].text
        assert "already passed" in gone
        one = (await client.call_tool("cancel_wakeup", {"which": "08:30"})).content[0].text
        assert one == "Cancelled the wake-up for 8:30 AM tomorrow."
        rest = (await client.call_tool("cancel_wakeup", {})).content[0].text
        assert rest == "Cancelled the wake-up for 7:50 AM tomorrow."
        none = (await client.call_tool("cancel_wakeup", {})).content[0].text
        assert none.startswith("No wake-up is set.") and "phone's Clock" in none
    assert reminders.open() == []


async def test_cancel_reminder_with_the_word_wake_also_works(tmp_path):
    reminders = Reminders(tmp_path / "r.json")
    wakeup.WakeUps(reminders, FakeMac(), tmp_path).add("2026-11-27 07:50", now=LATE_NIGHT)
    assert [r.title for r in reminders.cancel("wake")] == ["Wake-up"]


# ---------------------------------------------------------------- ringing


class FakeMac:
    """The laptop node as the server's hub sees it: volume, mute, quiet mode. Records every call; says nothing."""

    def __init__(self, awake=True, volume=20, muted=True, quiet=False, refuse=""):
        self.awake, self.volume, self.muted, self.quiet, self.refuse = awake, volume, muted, quiet, refuse
        self.calls: list[tuple[str, dict]] = []

    def reachable(self, node):
        return self.awake

    def said(self) -> list[str]:
        return [args["text"] for tool, args in self.calls if tool == "announce" and not self._refused(args)]

    def _refused(self, args) -> bool:
        return args.get("refused", False)

    async def call_on(self, node, tool, args):
        self.calls.append((tool, dict(args)))
        if not self.awake:
            return ToolOutput.failure("The laptop is asleep.")
        match tool:
            case "get_volume":
                return ToolOutput([], f"Volume {self.volume}%{' (muted)' if self.muted else ''}.", False)
            case "set_volume":
                self.volume, self.muted = args["level"], False
                return ToolOutput([], f"Volume {self.volume}%.", False)
            case "set_mute":
                self.muted = args["muted"]
                return ToolOutput([], "ok", False)
            case "set_quiet":
                self.quiet = args["on"]
                return ToolOutput([], "ok", False)
            case "announce":
                if self.refuse:
                    self.calls[-1][1]["refused"] = True
                    return ToolOutput.failure(self.refuse)
                if self.quiet:
                    self.calls[-1][1]["refused"] = True
                    return ToolOutput.failure("Didn't say it: quiet mode is on.")
                if self.muted or not self.volume:
                    self.calls[-1][1]["refused"] = True
                    return ToolOutput.failure("Didn't say it: the Mac is muted.")
                return ToolOutput([], f"Said: {args['text']}", False)
        return ToolOutput.failure(f"unknown {tool}")


def ringer(tmp_path, mac, busy_with="", headphones=False):
    reminders = Reminders(tmp_path / "r.json")
    wakes = wakeup.WakeUps(reminders, mac, tmp_path, user="Jashan", clock=lambda: DUE)
    told, said_first = [], []

    async def tell(text, buttons=None):
        told.append((text, buttons))

    async def busy(_now):
        return busy_with

    async def private():
        return headphones

    wakes.attach(tell, busy, private, lambda kind, text, route: said_first.append((kind, route)), log=lambda _: None)
    return wakes, reminders, told, said_first


async def fire(wakes, reminders, now):
    for reminder, late in reminders.take_due(now):
        await wakes.start(reminder, late, now)


def at(minute, second=0):
    return DUE.replace(minute=minute, second=second)


async def test_in_quiet_hours_it_turns_the_mac_up_says_it_three_times_then_puts_the_sound_back(tmp_path):
    mac = FakeMac(volume=20, muted=True)
    wakes, reminders, told, said_first = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert mac.said() == ["Good morning, Jashan. It's 7:50 AM. Time to get up."]
    assert ("set_volume", {"level": 50}) in mac.calls and mac.volume == 50 and not mac.muted
    assert told == [("⏰ Wake up! It's 7:50 AM.", wakeup.UP_BUTTON)]  # Telegram too, with the button
    await wakes.tick(at(51))
    assert len(mac.said()) == 1  # two minutes apart
    await wakes.tick(at(52))
    await wakes.tick(at(54))
    assert mac.said()[1:] == ["Jashan, it's 7:52 AM. Time to get up.", "Jashan, it's 7:54 AM. Time to get up."]
    assert [t for t, _ in told][1:] == ["⏰ Still time to get up: 7:52 AM (2 of 3).",
                                         "⏰ Still time to get up: 7:54 AM (3 of 3)."]  # fmt: skip
    assert wakes.ringing()
    await wakes.tick(at(55))  # a minute after the last ring: the sound goes back
    assert not wakes.ringing() and mac.volume == 20 and mac.muted
    assert said_first == [("wake-up 1/3", "spoken+telegram"), ("wake-up 2/3", "spoken+telegram"),
                          ("wake-up 3/3", "spoken+telegram"), ("wake-up end", "-")]  # fmt: skip
    await wakes.tick(at(58))
    assert len(mac.said()) == 3  # over


async def test_quiet_hours_do_not_stop_an_alarm_he_asked_for(tmp_path):
    from sommus import herald

    three_am = datetime(2026, 11, 27, 3, 0)
    assert herald.in_quiet_hours(three_am.time(), herald.clock_time("23:00"), herald.clock_time("07:30"))
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, _told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 03:00", now=LATE_NIGHT)
    await fire(wakes, reminders, three_am)
    assert mac.said() == ["Good morning, Jashan. It's 3:00 AM. Time to get up."]


async def test_a_loud_enough_mac_is_left_alone(tmp_path):
    mac = FakeMac(volume=70, muted=False)
    wakes, reminders, _told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert [tool for tool, _ in mac.calls] == ["get_volume", "announce"]


async def test_quiet_mode_is_lifted_for_the_alarm_and_put_back(tmp_path):
    mac = FakeMac(volume=60, muted=False, quiet=True)
    wakes, reminders, _told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert len(mac.said()) == 1 and mac.quiet is False
    await wakes.stop("answered")
    assert mac.quiet is True


async def test_in_a_class_it_is_never_said_out_loud_but_telegram_still_rings(tmp_path):
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, told, said_first = ringer(tmp_path, mac, busy_with="ECE 105 lecture")
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert mac.calls == [] and told[0][0] == "⏰ Wake up! It's 7:50 AM."
    assert said_first == [("wake-up 1/3", "telegram (busy: ECE 105 lecture)")]


async def test_in_a_class_with_headphones_on_it_is_said(tmp_path):
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, _told, _ = ringer(tmp_path, mac, busy_with="ECE 105 lecture", headphones=True)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert len(mac.said()) == 1


async def test_a_sleeping_mac_gets_nothing_and_telegram_says_it_anyway(tmp_path):
    mac = FakeMac(awake=False)
    wakes, reminders, told, said_first = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    for minute in (52, 54, 55):
        await wakes.tick(at(minute))
    assert mac.calls == [] and len(told) == 3
    assert [route for _, route in said_first[:3]] == ["telegram (Mac unreachable)"] * 3


async def test_a_mac_that_refuses_is_logged_with_why(tmp_path):
    mac = FakeMac(volume=60, muted=False, refuse="Didn't say it: nothing to say.")
    wakes, reminders, _told, said_first = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert said_first == [("wake-up 1/3", "telegram (not said: nothing to say)")]


async def test_say_out_loud_later_is_said_once_with_no_alarm_button(tmp_path):
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, told, said_first = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", "Time for the gym", once=True, now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert not wakes.ringing() and await wakes.heard("okay") == ""  # nothing to answer: "okay" is a request
    await wakes.tick(at(52))
    assert mac.said() == ["Time for the gym"] and told == [("🔊 Time for the gym", None)]
    assert said_first == [("said later", "spoken+telegram")] and wakes.active is None


async def test_a_late_ring_says_so(tmp_path):
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(58))  # the server was restarting
    assert told[0][0] == "⏰ Wake up! It's 7:58 AM. (meant for 7:50 AM; I was late)"


async def test_answering_stops_it_and_puts_the_sound_back(tmp_path):
    mac = FakeMac(volume=20, muted=True)
    wakes, reminders, _told, said_first = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert await wakes.heard("what's the weather", "Telegram") == ""  # a real request: it goes on, alarm off
    assert not wakes.ringing() and mac.volume == 20 and mac.muted
    await wakes.tick(at(52))
    assert len(mac.said()) == 1
    assert said_first[-1] == ("wake-up end", "-")


async def test_a_restart_mid_alarm_still_puts_the_sound_back(tmp_path):
    mac = FakeMac(volume=20, muted=False)
    wakes, reminders, _told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    again = wakeup.WakeUps(reminders, mac, tmp_path, clock=lambda: at(51))  # the server restarted
    assert again.ringing()
    again.attach(wakes.tell)
    await again.stop("answered")
    assert mac.volume == 20


async def test_cancel_while_ringing_stops_it(tmp_path):
    mac = FakeMac(volume=60, muted=False)
    wakes, reminders, _told, _ = ringer(tmp_path, mac)
    wakes.add("2026-11-27 07:50", now=LATE_NIGHT)
    await fire(wakes, reminders, at(50))
    assert await wakes.cancel() == "Alarm off."
    assert not wakes.ringing()


# ---------------------------------------------------------------- through the server


@pytest.fixture
def served(tmp_path, monkeypatch):
    from sommus.brain.nodes import NodeHub
    from sommus.brain.permissions import Policy
    from sommus.brain.store import Store
    from sommus.server import Phone, Server

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # brains are built but the model is fake
    mac = FakeMac(volume=60, muted=False)
    reminders = Reminders(tmp_path / "r.json")
    wakes = wakeup.WakeUps(reminders, mac, tmp_path, clock=lambda: DUE)
    hub = NodeHub((), Policy())
    s = Server(config(tmp_path), hub, Store(tmp_path / "s.db"), Phone(), log=lambda _: None,
               reminders=reminders, wakeups=wakes)  # fmt: skip
    told = []

    async def tell(text, buttons=None):
        told.append((text, buttons))

    async def free(_now):
        return ""

    wakes.attach(tell, free, None, s.record_said, log=lambda _: None)
    s.mac, s.told = mac, told
    return s


async def test_the_scheduler_rings_a_wake_up_instead_of_pinging_and_logs_where_it_went(served, tmp_path):
    served.wakeups.add("2026-11-27 07:50", now=LATE_NIGHT)
    await served.fire_reminders(at(49))
    assert served.mac.calls == []
    await served.fire_reminders(at(50))
    assert len(served.mac.said()) == 1 and served.told[0][0] == "⏰ Wake up! It's 7:50 AM."
    await served.fire_reminders(at(52))  # the scheduler's tick also does the repeats
    assert len(served.mac.said()) == 2
    note = activity.day_note("2026-11-27", [], activity.said_on(served.said_path, f"{datetime.now():%Y-%m-%d}"))
    assert "**Wake-ups:**" in note and "ring 1/3: spoken+telegram" in note


async def test_the_telegram_button_stops_it(served):
    served.wakeups.add("2026-11-27 07:50", now=LATE_NIGHT)
    await served.fire_reminders(at(50))
    assert await served.button_pressed("wake:up") == "Morning. Alarm off."
    assert not served.wakeups.ringing()
    assert await served.button_pressed("wake:up") == "That alarm is already off."


async def test_saying_im_up_to_any_brain_stops_it_for_free(served):
    served.wakeups.add("2026-11-27 07:50", now=LATE_NIGHT)
    await served.fire_reminders(at(50))
    served.brain.client = FakeModel()  # must not be asked
    reply = []
    async for event in served.brain.handle("ok I'm up", allow):
        reply.append(getattr(event, "text", ""))
    assert "".join(reply) == "Morning. Alarm off." and not served.wakeups.ringing()


async def test_a_real_request_while_it_rings_stops_it_and_still_gets_answered(served):
    served.wakeups.add("2026-11-27 07:50", now=LATE_NIGHT)
    await served.fire_reminders(at(50))
    served.voice_brain.client = FakeModel(text_reply("Sunny."))
    result = await served.ask("how should I dress today")
    assert result["reply"] == "Sunny." and not served.wakeups.ringing()


async def test_on_the_iphone_okay_reaches_the_alarm_instead_of_ending_the_shortcut(served):
    import httpx

    served.tailnet.is_mine = lambda address: True
    served.wakeups.add("2026-11-27 07:50", now=LATE_NIGHT)
    await served.fire_reminders(at(50))
    # in this thread (the turn log is SQLite), unlike starlette's TestClient
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served.app()), base_url="http://phone") as http:
        answer = (await http.post("/ask", json={"text": "okay thanks"})).json()
        assert answer["reply"] == "Morning. Alarm off." and not served.wakeups.ringing()
        again = (await http.post("/ask", json={"text": "okay thanks"})).json()
    assert again["action"] == "sleep"  # nothing rings: "okay thanks" ends the Shortcut, as before


async def test_the_fast_path_sets_one_through_the_server_brain_for_free(tmp_path, monkeypatch):
    from sommus.brain import loop
    from sommus.brain.nodes import NodeHub
    from sommus.brain.permissions import Policy
    from sommus.brain.store import Store
    from sommus.server import Phone, Server

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    real = wakeup.match
    monkeypatch.setattr(loop.wakeup, "match", lambda text, now=None: real(text, LATE_NIGHT))
    reminders = Reminders(tmp_path / "r.json")
    wakes = wakeup.WakeUps(reminders, FakeMac(), tmp_path, clock=lambda: LATE_NIGHT)
    async with NodeHub((), Policy()) as hub:
        await hub.add("wakeup", Client(wakes.node()))
        server = Server(config(tmp_path), hub, Store(tmp_path / "s.db"), Phone(), log=lambda _: None,
                        reminders=reminders, wakeups=wakes)  # fmt: skip
        server.brain.client = FakeModel()  # must not be asked
        reply = []
        async for event in server.brain.handle("wake me up at 7:50", allow):
            if type(event).__name__ == "TextDelta":
                reply.append(event.text)
    assert "".join(reply) == "Wake-up set for 7:50 AM tomorrow."
    assert [r.title for r in reminders.open()] == ["Wake-up"]


async def allow(_name, _args):
    return True
