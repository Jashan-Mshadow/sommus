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


def _js(tab: Tab, expression: str, timeout: float = 30) -> str:
    """Run JavaScript in a tab.

    AppleScript string literals can't contain real newlines, so the source is
    collapsed to one line first — a multi-line script silently returns nothing.
    """
    one_line = " ".join(expression.split())
    escaped = one_line.replace("\\", "\\\\").replace('"', '\\"')
    return _script(f'execute tab {tab.index} of window {tab.window} javascript "{escaped}"', timeout=timeout)


# D2L, Gmail and Google Docs build their UI from web components, so links live inside
# shadow roots that a plain querySelectorAll never sees. Both scripts below walk into them.
DEEP_WALK = r"""
function walk(root, out) {
  var sel = 'a[href], button, [role=link], [role=button], input[type=submit]';
  out.push.apply(out, Array.from(root.querySelectorAll(sel)));
  Array.from(root.querySelectorAll('*')).forEach(function (e) { if (e.shadowRoot) { walk(e.shadowRoot, out); } });
  return out;
}
function label(n) {
  return ((n.innerText || n.value || n.getAttribute('aria-label') || n.title || '').trim()).replace(/\s+/g, ' ');
}
"""

LINKS_JS = (
    DEEP_WALK
    + """
walk(document, []).map(function (n) {
  var t = label(n).slice(0, 120);
  return t ? t + ' -> ' + (n.href || '(button)') : '';
}).filter(Boolean).slice(0, %d).join('\\n')
"""
)

CLICK_JS = (
    DEEP_WALK
    + """
(function () {
  var needle = %s.toLowerCase();
  var hit = walk(document, []).find(function (n) { return label(n).toLowerCase().indexOf(needle) !== -1; });
  if (!hit) { return 'NOTFOUND'; }
  if (hit.href) { window.location.href = hit.href; return 'NAVIGATED ' + hit.href; }
  hit.click();
  return 'CLICKED ' + label(hit).slice(0, 80);
})()
"""
)


def list_links(query: str, contains: str | None = None, limit: int = 60) -> tuple[Tab, list[str]]:
    """Every link and button on the page as 'text -> url', so pages can be navigated by URL."""
    tab = find_tab(query)
    raw = _js(tab, LINKS_JS % max(limit * 4, 120))
    links = [line.strip() for line in raw.splitlines() if line.strip()]
    if contains:
        needle = contains.casefold()
        links = [line for line in links if needle in line.casefold()]
    return tab, links[:limit]


def click_link(query: str, text: str) -> tuple[Tab, str]:
    """Click a link or button by its visible text — what a mouse would do, without a mouse."""
    tab = find_tab(query)
    result = _js(tab, CLICK_JS % _json_string(text))
    if result.strip() == "NOTFOUND":
        _, links = list_links(query, limit=40)
        raise ActionError(
            f"Nothing on '{tab.title}' matching '{text}'. Links and buttons on the page:\n" + "\n".join(links[:25])
        )
    return tab, result.strip()


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)


GMAIL_COMPOSE = "https://mail.google.com/mail/u/0/?view=cm&fs=1"


def compose_gmail(to: str, subject: str, body: str, send: bool = False) -> str:
    """Open a pre-filled Gmail compose window; optionally press Cmd+Enter to send it."""
    import time
    from urllib.parse import quote

    from sommus.nodes.laptop.macos import _run, press_keys

    url = f"{GMAIL_COMPOSE}&to={quote(to)}&su={quote(subject)}&body={quote(body)}"
    _run(["open", url])
    if not send:
        return f"Draft open to {to}. Say send when you want it gone."
    time.sleep(4)  # the compose window has to exist before the keystroke lands
    press_keys("cmd+return")
    return f"Sent to {to}."


def focus_tab(query: str) -> Tab:
    tab = find_tab(query)
    _script(f"set active tab index of window {tab.window} to {tab.index}\nset index of window {tab.window} to 1")
    _osascript(f'tell application "{BROWSER}" to activate')
    return tab
