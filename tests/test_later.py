"""Small Mac actions asked while the Mac sleeps wait on the server and run when it wakes; nothing else does."""

from datetime import datetime, timedelta

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sommus.brain.later import Later
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy, Tier

NOW = datetime(2026, 10, 8, 13, 0)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def mac_node():
    ran: list[str] = []
    mac = MCPServer("laptop", log_level="WARNING")

    @mac.tool(annotations=REVERSIBLE, structured_output=False)
    def create_reminder(title: str, due: str | None = None) -> str:
        """Apple Reminders."""
        ran.append(f"reminder {title}")
        return f"Reminder added: {title}"

    @mac.tool(annotations=REVERSIBLE, structured_output=False)
    def open_url(url: str) -> str:
        """Open a page."""
        ran.append(f"open {url}")
        if "broken" in url:
            raise ToolError("Chrome said no")
        return f"Opened {url}"

    @mac.tool(annotations=REVERSIBLE, structured_output=False)
    def set_volume(level: int) -> str:
        """Reversible, but only makes sense now."""
        ran.append("volume")
        return "ok"

    @mac.tool(annotations=REVERSIBLE, structured_output=False)
    def find_contact(name: str) -> str:
        """Personal: behind the PIN."""
        ran.append("contact")
        return "Didi"

    @mac.tool(annotations=DESTRUCTIVE, structured_output=False)
    def quit_app(name: str) -> str:
        """Destructive."""
        ran.append("quit")
        return "quit"

    return mac, ran


class FakeMac:
    """Stands in for RemoteNode: asleep while `client` is None."""

    def __init__(self):
        self.client = None
        self.error = ""

    async def connected(self, wait: float = 0) -> Client | None:
        return self.client

    def drop(self) -> None:
        self.client = None


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def later(tmp_path, clock=None):
    # The list itself names a personal and a destructive tool: the code must still refuse both.
    listed = ["create_reminder", "open_url", "find_contact", "quit_app"]
    return Later(tmp_path / "mac_queue.json", listed, personal=["find_contact"], clock=clock or Clock(NOW))


async def hub_with_mac(hub: NodeHub, client: Client) -> FakeMac:
    mac = FakeMac()
    hub._remote["laptop"] = mac
    hub._register("laptop", (await client.list_tools()).tools)
    return mac


async def test_only_small_reversible_non_personal_actions_wait_for_the_mac(tmp_path):
    server, ran = mac_node()
    queue = later(tmp_path)
    async with NodeHub((), Policy(), later=queue) as hub, Client(server) as client:
        await hub_with_mac(hub, client)
        queued = await hub.call("create_reminder", {"title": "Buy milk"})
        assert not queued.is_error and "queued" in queued.text
        for tool, args in [
            ("set_volume", {"level": 20}),  # not on the list
            ("find_contact", {"name": "Didi"}),  # personal: never waits, even listed
            ("quit_app", {"name": "Chrome"}),  # destructive: never waits, even listed
        ]:
            refused = await hub.call(tool, args)
            assert refused.is_error and "isn't reachable" in refused.text, tool
    assert ran == []
    assert [(w.tool, w.args) for w in Later(tmp_path / "mac_queue.json", []).items] == [
        ("create_reminder", {"title": "Buy milk"})
    ]  # on disk: a server restart keeps it


async def test_a_permission_override_to_destructive_stops_it_waiting(tmp_path):
    server, _ran = mac_node()
    async with (
        NodeHub((), Policy({"open_url": Tier.DESTRUCTIVE}), later=later(tmp_path)) as hub,
        Client(server) as client,
    ):
        await hub_with_mac(hub, client)
        assert (await hub.call("open_url", {"url": "https://x.org"})).is_error


async def test_waiting_actions_run_once_when_the_mac_wakes_and_he_is_told(tmp_path):
    server, ran = mac_node()
    async with NodeHub((), Policy(), later=later(tmp_path)) as hub, Client(server) as client:
        mac = await hub_with_mac(hub, client)
        await hub.call("create_reminder", {"title": "Buy milk"})
        await hub.call("create_reminder", {"title": "Buy milk"})  # asked twice: queued once
        await hub.call("open_url", {"url": "https://broken.example"})
        assert await hub.run_waiting() == []  # still asleep: nothing runs, nothing is lost
        mac.client = client  # the Mac woke up
        told = await hub.run_waiting()
        assert await hub.run_waiting() == []  # never twice
    assert ran == ["reminder Buy milk", "open https://broken.example"]
    assert told[0] == "Your MacBook woke up, so I did what was waiting: create_reminder (Buy milk)."
    assert told[1].startswith("Waited for the MacBook but failed: open_url (https://broken.example)")
    assert "Chrome said no" in told[1]


async def test_an_action_that_waited_too_long_is_dropped_not_run(tmp_path):
    server, ran = mac_node()
    clock = Clock(NOW)
    async with NodeHub((), Policy(), later=later(tmp_path, clock)) as hub, Client(server) as client:
        mac = await hub_with_mac(hub, client)
        await hub.call("open_url", {"url": "https://x.org"})
        clock.now = NOW + timedelta(hours=30)
        mac.client = client
        told = await hub.run_waiting()
    assert ran == [] and told == ["Dropped, the MacBook was asleep too long: open_url (https://x.org)."]


async def test_if_the_mac_sleeps_again_mid_queue_the_rest_keeps_waiting(tmp_path):
    queue = later(tmp_path)
    queue.add("laptop", "create_reminder", {"title": "One"})
    queue.add("laptop", "create_reminder", {"title": "Two"})

    class Dozing(FakeMac):
        async def connected(self, wait: float = 0):
            return None

    async with NodeHub((), Policy(), later=queue) as hub:
        dozing = Dozing()
        dozing.client = object()  # looked awake, but every call finds it gone
        hub._remote["laptop"] = dozing
        assert await hub.run_waiting() == []
    assert [w.args["title"] for w in queue.items] == ["One", "Two"]


async def test_without_a_queue_the_mac_being_asleep_is_said_as_before(tmp_path):
    server, _ran = mac_node()
    async with NodeHub((), Policy()) as hub, Client(server) as client:
        await hub_with_mac(hub, client)
        result = await hub.call("create_reminder", {"title": "Buy milk"})
        assert result.is_error and "isn't reachable" in result.text
        assert await hub.run_waiting() == []


async def test_the_server_tells_telegram_what_ran(tmp_path):
    from types import SimpleNamespace

    from fakes import config

    from sommus.brain.store import Store
    from sommus.server import Phone, Server

    server, _ran = mac_node()
    said: list[tuple[int, str]] = []

    async def say(chat, text):
        said.append((chat, text))

    async with NodeHub((), Policy(), later=later(tmp_path)) as hub, Client(server) as client:
        mac = await hub_with_mac(hub, client)
        await hub.call("create_reminder", {"title": "Buy milk"})
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        sommus.bot = SimpleNamespace(allowed=[7], say=say)
        mac.client = client
        await sommus.run_waiting()
    assert said == [(7, "Your MacBook woke up, so I did what was waiting: create_reminder (Buy milk).")]
