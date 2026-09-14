"""App-level actions: what's playing, Shortcuts, Reminders.

These talk to other apps through Apple Events, so macOS asks for permission the
first time ("Terminal wants to control Reminders"). The errors say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sommus.nodes.laptop.macos import ActionError, _osascript, _run, running_apps

PLAYERS = ("Spotify", "Music")


def _is_running(app: str) -> bool:
    return any(a.name == app for a in running_apps())


def now_playing() -> str:
    for app in PLAYERS:
        if not _is_running(app):
            continue
        try:
            state = _osascript(f'tell application "{app}" to player state as text')
            if state != "playing":
                continue
            track = _osascript(f'tell application "{app}" to name of current track as text')
            artist = _osascript(f'tell application "{app}" to artist of current track as text')
            return f"{track} — {artist} (playing in {app})"
        except ActionError as e:
            if "not authorized" in str(e) or "1743" in str(e):
                raise ActionError(
                    f"macOS hasn't allowed access to {app} yet. Run the command again and click OK on the "
                    f"permission prompt, or enable it under Privacy & Security → Automation."
                ) from e
            raise
    open_players = [p for p in PLAYERS if _is_running(p)]
    if open_players:
        return f"{' and '.join(open_players)} " + ("is" if len(open_players) == 1 else "are") + " open but paused."
    return "No music app is running."


# ---------------------------------------------------------------- Contacts
# Names come from the Contacts app rather than a list in a config file, so new
# people work the moment they're saved on the phone.

CONTACTS_SCRIPT = [
    'set AppleScript\'s text item delimiters to ", "',
    'tell application "Contacts"',
    "set out to {}",
    "repeat with p in people",
    "set ph to {}",
    "repeat with x in phones of p",
    "set end of ph to value of x",
    "end repeat",
    "set em to {}",
    "repeat with x in emails of p",
    "set end of em to value of x",
    "end repeat",
    'set end of out to (name of p) & " | " & (ph as text) & " | " & (em as text)',
    "end repeat",
    "end tell",
    "set AppleScript's text item delimiters to linefeed",
    "return out as text",
]


@dataclass(frozen=True)
class Contact:
    name: str
    phones: list[str]
    emails: list[str]

    def __str__(self) -> str:
        parts = [self.name]
        if self.phones:
            parts.append("phone " + ", ".join(self.phones))
        if self.emails:
            parts.append("email " + ", ".join(self.emails))
        return " — ".join(parts)


def contacts() -> list[Contact]:
    try:
        raw = _osascript(*CONTACTS_SCRIPT, timeout=30)
    except ActionError as e:
        if "-1743" in str(e) or "not authorized" in str(e):
            raise ActionError(
                "macOS hasn't allowed access to Contacts yet — approve the prompt, or enable it under "
                "Privacy & Security → Automation."
            ) from e
        raise
    people = []
    for line in raw.splitlines():
        name, _, rest = line.partition(" | ")
        phones, _, emails = rest.partition(" | ")
        if name.strip():
            people.append(
                Contact(
                    name.strip(),
                    [p.strip() for p in phones.split(",") if p.strip()],
                    [e.strip() for e in emails.split(",") if e.strip()],
                )
            )
    return people


def find_contacts(query: str) -> list[Contact]:
    needle = query.casefold().strip()
    everyone = contacts()
    exact = [c for c in everyone if c.name.casefold() == needle]
    if exact:
        return exact
    parts = [c for c in everyone if needle in c.name.casefold()]
    if parts:
        return parts
    # "didi", "mom" and the like: match any word of the name, or the contact's own nickname
    return [c for c in everyone if any(word.startswith(needle) for word in c.name.casefold().split())]


# ---------------------------------------------------------------- Messages


def send_message(to: str, text: str) -> str:
    """Send an iMessage/SMS through Messages. Outward-facing: the brain always confirms first."""
    script = (
        'tell application "Messages" to send (item 2 of argv) to '
        "participant (item 1 of argv) of (1st account whose service type = iMessage)"
    )
    try:
        _osascript("on run argv", script, "end run", argv=(to, text), timeout=30)
    except ActionError as e:
        message = str(e)
        if "-1743" in message or "not authorized" in message:
            raise ActionError("macOS hasn't allowed control of Messages yet — approve the prompt and retry.") from e
        raise ActionError(
            f"Messages refused to send to '{to}' ({message.strip()}). Recent macOS restricts sending by "
            "script; try the exact phone number or Apple ID, or send it yourself."
        ) from e
    return to


# ---------------------------------------------------------------- Shortcuts
# The user's own Shortcuts are Sommus's escape hatch: anything macOS won't expose
# to a script (Focus modes, Home devices, app automations) can be a shortcut.


def list_shortcuts() -> list[str]:
    return [line.strip() for line in _run(["shortcuts", "list"]).splitlines() if line.strip()]


def run_shortcut(name: str) -> str:
    available = list_shortcuts()
    match = next((s for s in available if s.casefold() == name.casefold()), None)
    if match is None:
        close = [s for s in available if name.casefold() in s.casefold()]
        if len(close) == 1:
            match = close[0]
        else:
            raise ActionError(
                f"No shortcut named '{name}'." + (f" Close matches: {', '.join(close)}." if close else "")
            )
    _run(["shortcuts", "run", match], timeout=120)
    return match


# ---------------------------------------------------------------- Reminders
# Reminders survive Sommus restarting and sync to the iPhone, which makes them
# the right home for "remind me at 8pm" (unlike a notification, which is instant).


def create_reminder(title: str, due: str | None = None, list_name: str | None = None) -> str:
    when = ""
    if due:
        try:
            parsed = datetime.fromisoformat(due)
        except ValueError as e:
            raise ActionError(f"Couldn't read the time '{due}'. Use 2026-09-14 20:00 or 2026-09-14T20:00.") from e
        when = f', remind me date:date "{parsed:%-m/%-d/%Y %-I:%M:%S %p}"'
    target = f'list "{list_name}"' if list_name else "default list"
    script = (
        f'tell application "Reminders" to make new reminder at end of {target} '
        f"with properties {{name:(item 1 of argv){when}}}"
    )
    try:
        _osascript("on run argv", script, "end run", argv=(title,), timeout=30)
    except ActionError as e:
        if "-1743" in str(e) or "not authorized" in str(e):
            raise ActionError(
                "macOS hasn't allowed access to Reminders yet. Run it again and click OK on the prompt, "
                "or enable it under Privacy & Security → Automation."
            ) from e
        if "-1728" in str(e) and list_name:
            raise ActionError(f"There's no Reminders list called '{list_name}'.") from e
        raise
    return f"{title}{' at ' + due if due else ''}"


def list_reminders(limit: int = 10) -> list[str]:
    script = 'tell application "Reminders" to get name of (every reminder in every list whose completed is false)'
    try:
        raw = _osascript(script, timeout=30)
    except ActionError as e:
        if "-1743" in str(e) or "not authorized" in str(e):
            raise ActionError("macOS hasn't allowed access to Reminders yet — approve the prompt and retry.") from e
        raise
    names = [n.strip() for n in re.split(r",(?=\s)", raw) if n.strip()]
    return names[:limit]
