"""Reading and steering Chrome through AppleScript.

Tab titles and URLs work out of the box. Reading a page's *text* runs JavaScript
in the tab, which Chrome blocks until the user enables
View → Developer → Allow JavaScript from Apple Events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sommus.nodes.laptop.macos import ActionError, _osascript, running_apps

BROWSER = "Google Chrome"
SEPARATOR = "\x1f"  # AppleScript list joiner: never appears in a title or URL
MAX_PAGE_CHARS = 20_000

JS_BLOCKED_HINT = (
    "Chrome blocks JavaScript from scripts by default. In Chrome: View → Developer → "
    "Allow JavaScript from Apple Events. Titles and URLs work without it."
)


@dataclass(frozen=True)
class Tab:
    window: int
    index: int
    title: str
    url: str

    def __str__(self) -> str:
        return f"[{self.window}.{self.index}] {self.title} — {self.url}"


def _require_browser() -> None:
    if not any(a.name == BROWSER for a in running_apps()):
        raise ActionError(f"{BROWSER} isn't running.")


def _script(body: str, timeout: float = 20) -> str:
    try:
        return _osascript(f'tell application "{BROWSER}"', body, "end tell", timeout=timeout)
    except ActionError as e:
        message = str(e)
        if "JavaScript through AppleScript is turned off" in message:
            raise ActionError(JS_BLOCKED_HINT) from e
        if "-1743" in message or "not authorized" in message:
            raise ActionError(
                f"macOS hasn't allowed control of {BROWSER} yet — approve the prompt, or enable it under "
                "Privacy & Security → Automation."
            ) from e
        raise


def list_tabs() -> list[Tab]:
    _require_browser()
    tabs = []
    count = int(_script("get count of windows") or 0)
    for window in range(1, count + 1):
        titles = _script(
            f'set AppleScript\'s text item delimiters to "{SEPARATOR}"\nget title of tabs of window {window} as text'
        )
        urls = _script(
            f'set AppleScript\'s text item delimiters to "{SEPARATOR}"\nget URL of tabs of window {window} as text'
        )
        for index, (title, url) in enumerate(zip(titles.split(SEPARATOR), urls.split(SEPARATOR), strict=False), 1):
            tabs.append(Tab(window, index, title.strip(), url.strip()))
    return tabs


def find_tab(query: str) -> Tab:
    """Match a tab by number ("2"), "window.index" ("1.3"), or text in its title or URL."""
    tabs = list_tabs()
    if not tabs:
        raise ActionError(f"{BROWSER} has no open tabs.")
    if re.fullmatch(r"\d+\.\d+", query):
        window, index = (int(part) for part in query.split("."))
        found = [t for t in tabs if t.window == window and t.index == index]
    elif query.isdigit():
        found = [t for t in tabs if t.index == int(query) and t.window == 1]
    else:
        needle = query.casefold()
        found = [t for t in tabs if needle in t.title.casefold() or needle in t.url.casefold()]
    if not found:
        raise ActionError(f"No tab matching '{query}'. Open tabs:\n" + "\n".join(str(t) for t in tabs))
    return found[0]


def read_tab(query: str) -> tuple[Tab, str]:
    tab = find_tab(query)
    text = _script(f'execute tab {tab.index} of window {tab.window} javascript "document.body.innerText"', timeout=30)
    if not text.strip():
        raise ActionError(
            f"'{tab.title}' returned no text. PDFs and some viewers render outside the page — "
            "download the file instead (download_url) and read that."
        )
    return tab, text[:MAX_PAGE_CHARS] + ("\n[truncated]" if len(text) > MAX_PAGE_CHARS else "")


def focus_tab(query: str) -> Tab:
    tab = find_tab(query)
    _script(f"set active tab index of window {tab.window} to {tab.index}\nset index of window {tab.window} to 1")
    _osascript(f'tell application "{BROWSER}" to activate')
    return tab
