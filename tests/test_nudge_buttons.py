"""Tap-to-log buttons under the supplement nudge, and the nudge waiting out a class."""

from datetime import UTC, datetime
from types import SimpleNamespace

from fakes import config

from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.herald import Herald
from sommus.interfaces import telegram
from sommus.server import Phone, Server
from sommus.sleepmode import Sleep

BUTTONS = [[("✅ Took them", "supp:took"), ("Skip", "supp:skip")]]


class RecordingBot(telegram.Bot):
    def __init__(self, tmp_path, allowed=(42,)):
        self.allowed, self.sleep, self.calls = set(allowed), Sleep(tmp_path), []

    async def call(self, method, **params):
        self.calls.append((method, params))


async def test_buttons_go_under_the_last_part_of_a_long_message_only(tmp_path):
    bot = RecordingBot(tmp_path)
    await bot.say(42, "\n".join("x" * 90 for _ in range(100)), BUTTONS)
    sends = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(sends) > 1
    assert all("reply_markup" not in p for p in sends[:-1])
    keyboard = sends[-1]["reply_markup"]["inline_keyboard"]
    assert keyboard == [
        [{"text": "✅ Took them", "callback_data": "supp:took"}, {"text": "Skip", "callback_data": "supp:skip"}]
    ]


async def test_a_message_without_buttons_has_no_keyboard(tmp_path):
    bot = RecordingBot(tmp_path)
    await bot.say(42, "hello")
    assert "reply_markup" not in bot.calls[0][1]


async def test_a_tap_stops_the_spinner_removes_the_buttons_and_answers(tmp_path):
    bot = RecordingBot(tmp_path)
    seen = []

    async def on_button(data):
        seen.append(data)
        return "Logged creatine."

    bot.on_button = on_button
    tap = {"id": "cb1", "data": "supp:took", "message": {"message_id": 7, "chat": {"id": 42}}}
    await bot.button(tap, lambda line: None)
    methods = [m for m, _ in bot.calls]
    assert methods == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
    assert seen == ["supp:took"] and bot.calls[2][1]["text"] == "Logged creatine."


async def test_taps_from_strangers_or_while_asleep_do_nothing(tmp_path, monkeypatch):
    bot = RecordingBot(tmp_path)

    async def on_button(data):
        raise AssertionError("must not run")

    bot.on_button = on_button
    await bot.button({"id": "x", "data": "supp:took", "message": {"message_id": 1, "chat": {"id": 99}}}, print)
    monkeypatch.setenv("SOMMUS_SLEEP_PHRASE", "sleepmax override 0517")
    monkeypatch.setenv("SOMMUS_WAKE_PHRASE", "wakeupmax override 0517")
    bot.sleep.command("sleepmax override 0517")
    await bot.button({"id": "y", "data": "supp:took", "message": {"message_id": 1, "chat": {"id": 42}}}, print)
    assert bot.calls == []


async def test_the_herald_passes_buttons_only_when_there_are_some(tmp_path):
    told = []

    async def tell(text, buttons=None):
        told.append((text, buttons))

    async with NodeHub((), Policy()) as hub:
        herald = Herald(hub, tell, {"speak": False})
        now = datetime(2026, 11, 26, 17, 0)
        await herald.deliver("plain", "", now, kind="x")
        await herald.deliver("with buttons", "", now, kind="supplements", buttons=BUTTONS)
    assert told == [("plain", None), ("with buttons", BUTTONS)]


async def test_tapping_took_them_logs_what_the_nudge_listed_and_clears_it(tmp_path):
    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        calls = []

        async def fake_call(name, args):
            calls.append((name, args))
            return SimpleNamespace(text="Logged creatine and magnesium.", is_error=False)

        hub.call = fake_call
        sommus.state["supp_pending"] = {
            "items": ["creatine", "magnesium"],
            "scheduled": ["creatine", "magnesium", "fish oil"],
        }
        assert await sommus.button_pressed("supp:took") == "Logged creatine and magnesium."
        assert calls == [
            ("log_supplements", {"items": "creatine, magnesium", "scheduled": "creatine, magnesium, fish oil"})
        ]
        assert sommus.state["supp_pending"] == {}
        assert (
            await sommus.button_pressed("supp:took") == "Nothing is waiting to be logged."
        )  # a second tap does nothing
        sommus.state["supp_pending"] = {"items": ["creatine"]}
        assert await sommus.button_pressed("supp:skip") == "Okay, skipped." and sommus.state["supp_pending"] == {}


async def test_the_supplement_nudge_waits_out_a_class_then_comes_with_buttons(tmp_path, monkeypatch):

    from sommus import server as server_module

    settings = {
        "enabled": True,
        "log": "Nutrition/Supplement Log.md",
        "window_minutes": 120,
        "slots": [{"name": "morning", "at": "10:00", "say": "morning", "items": ["multivitamin", "hair gummies"]}],
    }
    real = server_module.config.section
    monkeypatch.setattr(server_module.config, "section", lambda name: settings if name == "supplements" else real(name))
    (tmp_path / "vault" / "Nutrition").mkdir(parents=True)
    (tmp_path / "vault" / "Nutrition" / "Supplement Log.md").write_text("# Supplement log\n")
    monkeypatch.setenv("SOMMUS_VAULT_PATH", str(tmp_path / "vault"))
    delivered = []

    async def deliver(text, spoken, now, **kwargs):
        delivered.append((text, kwargs))
        return False

    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        sommus.herald.deliver = deliver
        in_class = ["MATH 115 lecture"]

        async def busy(now):
            return in_class[0]

        sommus.busy = busy
        now = datetime(2026, 11, 26, 10, 5, tzinfo=UTC)
        await sommus.supplement_nudges(now)
        assert delivered == [] and not sommus.state.get("supplements")  # nothing sent, slot not used up
        in_class[0] = ""
        await sommus.supplement_nudges(now.replace(hour=11, minute=40))  # class over, still inside the window
        assert len(delivered) == 1 and delivered[0][1]["buttons"] == BUTTONS
        assert sommus.state["supp_pending"]["items"] == ["multivitamin", "hair gummies"]
