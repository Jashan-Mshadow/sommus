"""`sommus node`: the Mac's own tools, served to the always-on brain over the tailnet.

The brain lives on the server now; what only the Mac can do — apps, volume, the screen, Chrome, iMessage,
Contacts, Claude Code's calendar — stays here, as the same MCP servers the brain used to launch itself,
now reached over streamable HTTP. It listens only on the Mac's tailnet address and wants a bearer token, so
nothing on eduroam can reach it. Run it from Terminal.app: macOS gives the permissions (Accessibility,
Contacts, Automation) to the app that launched it, which is also why it can't be a LaunchAgent.
"""

from __future__ import annotations

import contextlib
import hmac
import importlib
import os
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from sommus import config


class BearerToken:
    """Every request must carry `Authorization: Bearer <SOMMUS_NODE_TOKEN>`."""

    def __init__(self, app: ASGIApp, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def mac_nodes(cfg: config.Config) -> list[config.NodeConfig]:
    return [n for n in cfg.nodes if n.where == "mac"]


def build_app(cfg: config.Config, token: str, host: str) -> Starlette:
    from mcp.server.transport_security import TransportSecuritySettings

    servers = []
    routes = []
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[host, f"{host}:*"])
    for node in mac_nodes(cfg):
        os.environ.update(node.env)  # each node reads its settings from the environment at import
        server = importlib.import_module(node.module).server
        servers.append(server)
        app = server.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=host)
        routes.append(Mount(f"/{node.name}", app=app))

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Mounted apps don't get their own lifespan, so their session managers start here.
        async with contextlib.AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(BearerToken, token=token)])


def serve(cfg: config.Config, log=print) -> None:
    import uvicorn

    token = os.environ.get("SOMMUS_NODE_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("SOMMUS_NODE_TOKEN is missing from .env (the server has the same one).")
    settings = config.deploy()
    host, port = settings["mac_host"], int(settings["mac_port"])
    app = build_app(cfg, token, host)
    names = ", ".join(n.name for n in mac_nodes(cfg))
    log(f"Serving {names} to the server on {host}:{port} (tailnet only). Keep this window open; Ctrl+C stops it.")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def serve_in_background(cfg: config.Config):
    """`sommus live` doing `sommus node`'s job, so one window keeps both: returns the server task, or None with
    the reason (no token, or a separate `sommus node` already serving)."""
    import asyncio

    import uvicorn

    token = os.environ.get("SOMMUS_NODE_TOKEN", "")
    settings = config.deploy()
    host, port = settings["mac_host"], int(settings["mac_port"])
    if len(token) < 32:
        return None, "no SOMMUS_NODE_TOKEN in .env"
    if port_in_use(host, port):
        return None, "a separate `sommus node` is already serving the Mac's tools"

    class QuietServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # Ctrl+C belongs to live mode, not to this background server
            yield

    server = QuietServer(uvicorn.Config(build_app(cfg, token, host), host=host, port=port, log_level="warning"))
    return BackgroundNode(server, asyncio.create_task(server.serve())), f"serving the Mac's tools on {host}:{port}"


class BackgroundNode:
    def __init__(self, server, task):
        self.server, self.task = server, task

    async def stop(self) -> None:
        """Let uvicorn finish its open requests and close; cancelling it mid-request printed a traceback."""
        import asyncio

        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 5)
        except (TimeoutError, asyncio.CancelledError, Exception):
            self.task.cancel()
