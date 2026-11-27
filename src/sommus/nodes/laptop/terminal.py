"""Driving Terminal.app: list its tabs, read one, open a tab or window running a command, send it text or keys,
close a finished one.

Safety, in code:
- The window Sommus itself runs in is never touched: the tab whose tty belongs to `sommus live` (its pid is in
  ~/.sommus/live.pid), the tab this node runs under, and any window titled with `sommus-live.command`. They are
  listed, marked, and every other action refuses them.
- A busy tab (a program still running) is never closed.
- When several tabs match a title, nothing is sent: the caller must pick one by number.
The PIN and the permission tiers live in the brain (config.toml [security] pin_tools; server.py annotations).

Terminal's AppleScript covers most of this with Automation permission only. Ctrl+C / Ctrl+Z / Ctrl+\\ are sent as
the signals those keys raise, to the tab's foreground processes, so they need nothing more. Other keys, typing
without Return and new tabs are real keystrokes, which need Accessibility for the app Sommus runs in.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, replace
from pathlib import Path

from sommus.nodes.laptop import macos
from sommus.nodes.laptop.macos import ActionError

FIELD = "\x1f"
RECORD = "\x1e"
LIVE_MARK = "sommus-live.command"
LIVE_PID_FILES = (Path.home() / ".sommus" / "live.pid",)
MAX_LINES = 300
MAX_CHARS = 12_000

LIST_SCRIPT = (
    "on run",
    "set fs to ASCII character 31",
    "set rs to ASCII character 30",
    'set out to ""',
    'tell application "Terminal"',
    "set wi to 0",
    "repeat with w in windows",
    "set wi to wi + 1",
    "try",
    "set wname to name of w",
    "set wid to id of w",
    "set wfront to frontmost of w",
    "set ti to 0",
    "repeat with t in tabs of w",
    "set ti to ti + 1",
    'set AppleScript\'s text item delimiters to ","',
    "set procs to (processes of t) as text",
    'set AppleScript\'s text item delimiters to ""',
    "set ct to custom title of t",
    'if ct is missing value then set ct to ""',
    "set out to out & wi & fs & ti & fs & wid & fs & wname & fs & ct & fs & (busy of t) & fs & procs & fs & "
    "(tty of t) & fs & (selected of t) & fs & wfront & rs",
    "end repeat",
    "end try",
    "end repeat",
    "end tell",
    "return out",
    "end run",
)

TAB = "tab (item 2 of argv as integer) of window id (item 1 of argv as integer)"

HISTORY_SCRIPT = ("on run argv", 'tell application "Terminal" to get history of ' + TAB, "end run")
DO_SCRIPT = ("on run argv", f'tell application "Terminal" to do script (item 3 of argv) in {TAB}', "end run")
NEW_WINDOW_SCRIPT = (
    "on run argv",
    'tell application "Terminal"',
    "set t to do script (item 1 of argv)",
    "activate",
    "return tty of t",
    "end tell",
    "end run",
)
FOCUS_SCRIPT = (
    "on run argv",
    'tell application "Terminal"',
    "set w to window id (item 1 of argv as integer)",
    "set selected of tab (item 2 of argv as integer) of w to true",
    "set frontmost of w to true",
    "set index of w to 1",
    "activate",
    "end tell",
    "end run",
)
FRONT_TTY_SCRIPT = ('tell application "Terminal" to get tty of selected tab of front window',)
CLOSE_WINDOW_SCRIPT = ("on run argv", 'tell application "Terminal" to close window id (item 1 of argv as integer)',
                       "end run")  # fmt: skip


@dataclass(frozen=True)
class Tab:
    number: int  # 1.. across every window, in Terminal's front-to-back window order
    window: int  # window position, 1 = front
    tab: int
    window_id: int
    title: str
    custom_title: str
    busy: bool
    processes: tuple[str, ...]
    tty: str
    selected: bool
    front_window: bool
    protected: bool = False

    @property
    def label(self) -> str:
        return f"{self.window}.{self.tab}"

    @property
    def running(self) -> list[str]:
        """What's running beyond the login shell, for the listing."""
        skip = {"login", "-zsh", "zsh", "-bash", "bash", "sh", "-sh", "fish"}
        return [p for p in self.processes if p not in skip]

    def __str__(self) -> str:
        state = "busy" if self.busy else ("finished" if not self.processes else "idle")
        running = f" ({', '.join(self.running)})" if self.running else ""
        name = self.custom_title or self.title
        marks = (" [in front]" if self.selected and self.front_window else "") + (
            " [Sommus runs here: hands off]" if self.protected else ""
        )
        return f"[{self.number}] window {self.label} '{name}' — {state}{running}, {self.tty}{marks}"


def _terminal(*lines: str, argv: tuple[str, ...] = (), timeout: float = 15) -> str:
    try:
        return macos._osascript(*lines, argv=argv, timeout=timeout)
    except ActionError as e:
        text = str(e)
        if "-1743" in text or "Not authorized to send Apple events" in text:
            from sommus.nodes.laptop.windows import host_app

            raise ActionError(
                f"macOS hasn't let {host_app()} control Terminal yet. Turn it on in System Settings → Privacy & "
                f"Security → Automation → {host_app()} → Terminal."
            ) from e
        if "-600" in text or "isn't running" in text:
            raise ActionError("Terminal isn't running.") from e
        raise


def _tty_of(pid: int) -> str:
    try:
        tty = macos._run(["ps", "-o", "tty=", "-p", str(pid)], timeout=5).strip()
    except ActionError:
        return ""
    return f"/dev/{tty}" if tty and tty not in {"??", "-"} else ""


def protected_ttys() -> set[str]:
    """The ttys of `sommus live` and of whatever started this node: never typed into, never closed."""
    found = {_tty_of(os.getpid())}
    for path in LIVE_PID_FILES:
        try:
            found.add(_tty_of(int(path.read_text().strip())))
        except (OSError, ValueError):
            continue
    return {t for t in found if t}


def parse_tabs(raw: str, protected: set[str] | frozenset[str] = frozenset()) -> list[Tab]:
    rows = []
    for record in raw.split(RECORD):
        parts = record.strip("\n").split(FIELD)
        if len(parts) < 10:
            continue
        window, tab, window_id, title, custom, busy, procs, tty, selected, front = parts[:10]
        try:
            rows.append(
                Tab(
                    number=0,
                    window=int(window),
                    tab=int(tab),
                    window_id=int(window_id),
                    title=title.strip(),
                    custom_title=custom.strip(),
                    busy=busy.strip() == "true",
                    processes=tuple(p.strip() for p in procs.split(",") if p.strip()),
                    tty=tty.strip(),
                    selected=selected.strip() == "true",
                    front_window=front.strip() == "true",
                )
            )
        except ValueError:
            continue
    # The whole window is off limits when any tab in it is Sommus's, or its title names the live command.
    guarded = {r.window_id for r in rows if r.tty in protected or LIVE_MARK in r.title.casefold()}
    return [replace(r, number=n, protected=r.window_id in guarded) for n, r in enumerate(rows, 1)]


def list_tabs() -> list[Tab]:
    return parse_tabs(_terminal(*LIST_SCRIPT, timeout=20), protected_ttys())


def describe(tabs: list[Tab]) -> str:
    if not tabs:
        return "No Terminal windows are open."
    busy = sum(t.busy for t in tabs)
    return f"{len(tabs)} Terminal tab(s), {busy} busy:\n" + "\n".join(f"- {t}" for t in tabs)


CURRENT = {"", "current", "this", "front", "active", "that", "this one", "the front one", "current tab"}


def find_tab(tabs: list[Tab], query: str) -> Tab:
    """By number ("3"), window.tab ("2.1"), "current" (the front window's tab), or text in its title or the
    processes running in it. Text matching several tabs is refused: sending to the wrong one could do harm."""
    if not tabs:
        raise ActionError("No Terminal windows are open.")
    wanted = query.strip().casefold()
    listing = "\n".join(f"- {t}" for t in tabs)
    if wanted in CURRENT:
        found = [t for t in tabs if t.front_window and t.selected] or [t for t in tabs if t.window == 1 and t.selected]
    elif re.fullmatch(r"\d+\.\d+", wanted):
        window, tab = (int(p) for p in wanted.split("."))
        found = [t for t in tabs if t.window == window and t.tab == tab]
    elif wanted.isdigit():
        found = [t for t in tabs if t.number == int(wanted)]
    else:
        found = [
            t
            for t in tabs
            if wanted in t.title.casefold()
            or wanted in t.custom_title.casefold()
            or any(wanted in p.casefold() for p in t.processes)
        ]
        usable = [t for t in found if not t.protected]
        if len(usable) > 1:
            raise ActionError(f"'{query}' matches {len(usable)} tabs; pick one by number:\n" + listing)
        found = usable or found
    if not found:
        raise ActionError(f"No Terminal tab matching '{query}'. Tabs:\n" + listing)
    return found[0]


def _usable(tab: Tab) -> Tab:
    if tab.protected:
        raise ActionError(
            f"Tab {tab.number} is the Terminal window Sommus itself runs in ({LIVE_MARK}). I won't touch it: "
            "pick another tab."
        )
    return tab


def _ids(tab: Tab) -> tuple[str, str]:
    return str(tab.window_id), str(tab.tab)


def last_lines(history: str, lines: int) -> str:
    """The last `lines` lines with text, trailing blank screen rows dropped, capped in characters."""
    rows = history.rstrip().splitlines()
    text = "\n".join(rows[-lines:])
    return text[-MAX_CHARS:]


def read(query: str, lines: int = 40) -> tuple[Tab, str]:
    tab = _usable(find_tab(list_tabs(), query))
    count = max(1, min(int(lines), MAX_LINES))
    return tab, last_lines(_terminal(*HISTORY_SCRIPT, argv=_ids(tab), timeout=20), count)


# ---------------------------------------------------------------- sending

# Keys that are really signals to the program in front: sent straight to its processes, no keystroke needed.
SIGNAL_KEYS = {"ctrl+c": signal.SIGINT, "ctrl+z": signal.SIGTSTP, "ctrl+\\": signal.SIGQUIT}


def normal_keys(keys: str) -> str:
    """'Control-C', '^C', 'ctrl c' -> 'ctrl+c'; 'Enter' -> 'return'."""
    text = keys.strip().lower()
    text = re.sub(r"^\^(.)$", r"ctrl+\1", text)
    text = re.sub(r"\bcontrol\b", "ctrl", text)
    text = re.sub(r"\s*[-+ ]\s*", "+", text) if text not in {"+", "-"} else text
    return {"enter": "return"}.get(text, text)


def foreground_pids(ps_output: str) -> list[int]:
    """From `ps -t <tty> -o pid=,stat=`: the processes in the terminal's foreground group ('+' in stat)."""
    pids = []
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "+" in parts[1] and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


def _signal(tab: Tab, sig: signal.Signals) -> int:
    short = tab.tty.removeprefix("/dev/")
    pids = foreground_pids(macos._run(["ps", "-t", short, "-o", "pid=,stat="], timeout=5))
    sent = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent += 1
        except (ProcessLookupError, PermissionError):
            continue
    return sent


def _focus(tab: Tab) -> None:
    """Bring exactly this tab to the front, and check it is there before any keystroke goes out."""
    _terminal(*FOCUS_SCRIPT, argv=_ids(tab))
    time.sleep(0.4)
    front = _terminal(*FRONT_TTY_SCRIPT).strip()
    if front != tab.tty or (macos.frontmost_app() or "") != "Terminal":
        raise ActionError(f"Couldn't bring tab {tab.number} to the front, so nothing was typed.")


def refuse_sudo(command: str) -> None:
    if any(command.strip().startswith(b) for b in macos.BLOCKED_COMMANDS):
        raise ActionError("Commands needing sudo aren't sent from here: type that one yourself.")


def send(query: str, text: str = "", keys: str = "", press_return: bool = True) -> str:
    if bool(text) == bool(keys):
        raise ActionError("Send either text or keys, not both and not neither.")
    tab = _usable(find_tab(list_tabs(), query))
    if text:
        refuse_sudo(text)
        if "\n" in text or "\r" in text:
            raise ActionError("Send one line at a time.")
        if press_return:
            _terminal(*DO_SCRIPT, argv=(*_ids(tab), text))
            return f"Sent '{text}' to tab {tab.number} and pressed Return."
        _focus(tab)
        macos.type_text(text)
        return f"Typed '{text}' into tab {tab.number} (no Return)."
    combo = normal_keys(keys)
    if combo in SIGNAL_KEYS:
        if not tab.busy:
            return f"Tab {tab.number} isn't running anything, so there's nothing for {combo} to stop."
        count = _signal(tab, SIGNAL_KEYS[combo])
        return f"Sent {combo} to tab {tab.number} ({count} process{'es' if count != 1 else ''})."
    if combo == "return":
        _terminal(*DO_SCRIPT, argv=(*_ids(tab), ""))
        return f"Pressed Return in tab {tab.number}."
    _focus(tab)
    macos.press_keys(combo)
    return f"Pressed {combo} in tab {tab.number}."


# ---------------------------------------------------------------- opening and closing


def shell_line(command: str, folder: str) -> str:
    path = Path(folder or "~").expanduser()
    if not path.is_dir():
        raise ActionError(f"'{folder}' isn't a folder.")
    line = f"cd {shlex.quote(str(path))}"
    return f"{line} && {command.strip()}" if command.strip() else line


def open_tab(command: str = "", folder: str = "~", new_tab: bool = False) -> str:
    refuse_sudo(command)
    if "\n" in command:
        raise ActionError("One command line only (join steps with &&).")
    line = shell_line(command, folder)
    doing = f" running `{command.strip()}`" if command.strip() else ""
    where = Path(folder or "~").expanduser()
    if new_tab:
        before = list_tabs()
        host = next((t for t in before if t.front_window and t.selected and not t.protected), None)
        host = host or next((t for t in before if t.selected and not t.protected), None)
        if host is not None:
            _focus(host)
            macos.press_keys("cmd+t")
            time.sleep(0.6)
            old = {t.tty for t in before}
            fresh = next((t for t in list_tabs() if t.tty not in old and t.window_id == host.window_id), None)
            if fresh is None or fresh.protected:
                raise ActionError("The new tab didn't appear, so the command wasn't sent anywhere.")
            _terminal(*DO_SCRIPT, argv=(*_ids(fresh), line))
            return f"Opened a new tab (tab {fresh.number}) in {where}{doing}."
    tty = _terminal(*NEW_WINDOW_SCRIPT, argv=(line,)).strip()
    return f"Opened a new Terminal window in {where}{doing} ({tty})."


def close(query: str) -> str:
    tabs = list_tabs()
    tab = _usable(find_tab(tabs, query))
    if tab.busy:
        running = ", ".join(tab.running) or "a program"
        raise ActionError(
            f"Tab {tab.number} is still running {running}. I only close finished tabs: stop it first (ctrl+c) or "
            "let it finish."
        )
    siblings = [t for t in tabs if t.window_id == tab.window_id]
    if len(siblings) == 1:
        _terminal(*CLOSE_WINDOW_SCRIPT, argv=(str(tab.window_id),))
        return f"Closed tab {tab.number} (its window)."
    if tab.processes:  # an idle shell: end it, and Terminal closes the tab when its profile says so
        _terminal(*DO_SCRIPT, argv=(*_ids(tab), "exit"))
        time.sleep(1.0)
        after = next((t for t in list_tabs() if t.tty == tab.tty and t.window_id == tab.window_id), None)
        if after is None:
            return f"Closed tab {tab.number}."
        tab = after
        if tab.busy or tab.protected:
            raise ActionError(f"Tab {tab.number} didn't close.")
    _focus(tab)  # a tab whose shell has ended: Terminal only closes it with Cmd+W
    macos.press_keys("cmd+w")
    return f"Closed tab {tab.number}."
