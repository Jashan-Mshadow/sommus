"""Window management: list, snap, move, resize, minimize, hide, side by side, other display.

Windows are read and moved through System Events (the Accessibility API), which needs the app Sommus runs in
(Terminal for `sommus live`) to have Accessibility permission. Without it, listing falls back to Quartz's window
list (on-screen windows only, no minimized ones), and moving fails with a sentence that says which switch to turn
on, never a raw AppleScript error.

Coordinates are macOS "points" with the origin at the top-left of the main display (the one with the menu bar),
y growing downwards: what System Events uses. Displays come from NSScreen through JXA, run fresh each call so a
monitor plugged in after Sommus started is still seen. Snapping uses each display's visible frame: the screen
minus the menu bar and the Dock.

Every name from the model reaches AppleScript through argv, never by string formatting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from sommus.nodes.laptop import macos
from sommus.nodes.laptop.macos import ActionError

FIELD = "\x1f"  # between fields: never in a title
RECORD = "\x1e"  # between windows

Position = Literal[
    "left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right", "maximize", "center",
    "fullscreen",
]  # fmt: skip
POSITIONS: tuple[str, ...] = Position.__args__  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def contains(self, point: tuple[float, float]) -> bool:
        px, py = point
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def overlap(self, other: Rect) -> int:
        dx = min(self.x + self.w, other.x + other.w) - max(self.x, other.x)
        dy = min(self.y + self.h, other.y + other.h) - max(self.y, other.y)
        return max(dx, 0) * max(dy, 0)


@dataclass(frozen=True)
class Display:
    number: int  # 1 = the main display (menu bar)
    name: str
    frame: Rect
    visible: Rect  # minus the menu bar and the Dock

    @property
    def built_in(self) -> bool:
        return "built-in" in self.name.casefold() or "built in" in self.name.casefold()

    def __str__(self) -> str:
        return f"display {self.number} ({self.name}, {self.frame.w}x{self.frame.h})"


@dataclass(frozen=True)
class Window:
    app: str
    index: int  # within its app, 1 = the app's front window
    title: str
    rect: Rect
    minimized: bool = False
    app_front: bool = False  # its app is the frontmost app
    display: int | None = None

    def __str__(self) -> str:
        where = "minimized" if self.minimized else f"at {self.rect.x},{self.rect.y} size {self.rect.w}x{self.rect.h}"
        on = f", display {self.display}" if self.display and not self.minimized else ""
        title = f" '{self.title}'" if self.title else ""
        front = " [front app]" if self.app_front and self.index == 1 else ""
        return f"{self.app} #{self.index}{title}: {where}{on}{front}"


# ---------------------------------------------------------------- permission errors


def host_app() -> str:
    """The app whose permissions macOS checks: the terminal Sommus was started from."""
    program = os.environ.get("TERM_PROGRAM", "")
    return {
        "Apple_Terminal": "Terminal",
        "iTerm.app": "iTerm",
        "vscode": "Visual Studio Code",
        "WarpTerminal": "Warp",
        "ghostty": "Ghostty",
    }.get(program, "Terminal")


def accessibility_hint() -> str:
    app = host_app()
    return (
        f"macOS blocked this: moving and resizing windows needs Accessibility permission for {app}. Turn it on in "
        f"System Settings → Privacy & Security → Accessibility → {app}, then restart sommus live."
    )


def automation_hint() -> str:
    app = host_app()
    return (
        f"macOS hasn't let {app} control System Events yet. Turn it on in System Settings → Privacy & Security → "
        f"Automation → {app} → System Events, then try again."
    )


def plain(error: ActionError) -> ActionError:
    """An AppleScript failure as a sentence about the missing permission, or unchanged."""
    text = str(error)
    if "assistive access" in text or "-25211" in text or "-1719" in text:
        return ActionError(accessibility_hint())
    if "-1743" in text or "Not authorized to send Apple events" in text:
        return ActionError(automation_hint())
    return error


def _system_events(*lines: str, argv: tuple[str, ...] = (), timeout: float = 15) -> str:
    try:
        return macos._osascript(*lines, argv=argv, timeout=timeout)
    except ActionError as e:
        raise plain(e) from e


def _jxa(script: str) -> str:
    return macos._run(["osascript", "-l", "JavaScript", "-e", script], timeout=10)


# ---------------------------------------------------------------- displays

DISPLAYS_JXA = """
ObjC.import('AppKit');
var s = $.NSScreen.screens, out = [];
for (var i = 0; i < s.count; i++) {
  var d = s.objectAtIndex(i), f = d.frame, v = d.visibleFrame, n = '';
  try { n = d.localizedName.js; } catch (e) {}
  out.push([f.origin.x, f.origin.y, f.size.width, f.size.height,
            v.origin.x, v.origin.y, v.size.width, v.size.height, n].join(','));
}
out.join('\\n');
"""


def parse_displays(raw: str) -> list[Display]:
    """NSScreen frames (origin bottom-left of the main display, y up) -> top-left rectangles (y down).
    The first screen is the main one; its height flips every other screen's y."""
    rows = [line.split(",", 8) for line in raw.strip().splitlines() if line.strip()]
    if not rows:
        return []
    main_height = float(rows[0][3])
    found = []
    for number, row in enumerate(rows, 1):
        fx, fy, fw, fh, vx, vy, vw, vh = (float(v) for v in row[:8])
        name = row[8].strip() if len(row) > 8 and row[8].strip() else f"display {number}"
        frame = Rect(round(fx), round(main_height - (fy + fh)), round(fw), round(fh))
        visible = Rect(round(vx), round(main_height - (vy + vh)), round(vw), round(vh))
        found.append(Display(number, name, frame, visible))
    return found


def displays() -> list[Display]:
    found = parse_displays(_jxa(DISPLAYS_JXA))
    if not found:
        raise ActionError("Couldn't read the displays.")
    return found


def display_of(rect: Rect, screens: list[Display]) -> Display | None:
    """The display holding the window's centre, else the one it overlaps most."""
    for screen in screens:
        if screen.frame.contains(rect.center):
            return screen
    best = max(screens, key=lambda s: s.frame.overlap(rect), default=None)
    return best if best is not None and best.frame.overlap(rect) > 0 else None


def pick_display(query: str, screens: list[Display], current: Display | None = None) -> Display:
    """'' = where the window is; '2'; 'main'; 'built-in' / 'laptop'; 'external' / 'monitor'; 'next' / 'other';
    or part of the display's name."""
    wanted = query.strip().casefold().replace("_", "-")
    if not wanted:
        return current or screens[0]
    listing = ", ".join(str(s) for s in screens)
    if wanted in {"next", "other", "the other one", "other display", "other screen"}:
        if len(screens) < 2:
            raise ActionError("Only one display is connected.")
        at = screens.index(current) if current in screens else 0
        return screens[(at + 1) % len(screens)]
    if wanted.isdigit():
        for screen in screens:
            if screen.number == int(wanted):
                return screen
        raise ActionError(f"There's no display {wanted}. Displays: {listing}.")
    if wanted in {"main", "primary", "first"}:
        return screens[0]
    if wanted in {"built-in", "builtin", "built in", "laptop", "internal", "macbook"}:
        found = [s for s in screens if s.built_in]
        if found:
            return found[0]
        raise ActionError(f"No built-in display is active. Displays: {listing}.")
    if wanted in {"external", "monitor", "external monitor", "second", "big"}:
        found = [s for s in screens if not s.built_in]
        if found:
            return found[0]
        raise ActionError(f"No external display is connected. Displays: {listing}.")
    found = [s for s in screens if wanted in s.name.casefold()]
    if found:
        return found[0]
    raise ActionError(f"No display matching '{query}'. Displays: {listing}.")


# ---------------------------------------------------------------- listing

LIST_SCRIPT = (
    "on run",
    "set fs to ASCII character 31",
    "set rs to ASCII character 30",
    'set out to ""',
    'tell application "System Events"',
    # Outside any try: without Accessibility this fails loudly. Inside the loop's try it read as "no windows".
    "count windows of (first process whose frontmost is true)",
    "repeat with p in (every process whose background only is false)",
    "set pname to name of p",
    "set pfront to frontmost of p",
    "try",
    "set i to 0",
    "repeat with w in (every window of p)",
    "set i to i + 1",
    "try",
    'set t to ""',
    "try",
    "set t to name of w",
    "end try",
    'if t is missing value then set t to ""',
    "set mini to false",
    "try",
    'set mini to value of attribute "AXMinimized" of w',
    "end try",
    "set {x, y} to position of w",
    "set {ww, hh} to size of w",
    "set out to out & pname & fs & i & fs & t & fs & x & fs & y & fs & ww & fs & hh & fs & mini & fs & pfront & rs",
    "end try",
    "end repeat",
    "end try",
    "end repeat",
    "end tell",
    "return out",
    "end run",
)


def parse_windows(raw: str, screens: list[Display]) -> list[Window]:
    found = []
    for record in raw.split(RECORD):
        parts = record.strip("\n").split(FIELD)
        if len(parts) < 9:
            continue
        app, index, title, x, y, w, h, mini, front = parts[:9]
        try:
            rect = Rect(round(float(x)), round(float(y)), round(float(w)), round(float(h)))
        except ValueError:
            continue
        shown = display_of(rect, screens)
        found.append(
            Window(
                app=app.strip(),
                index=int(index),
                title=title.strip(),
                rect=rect,
                minimized=mini.strip() == "true",
                app_front=front.strip() == "true",
                display=shown.number if shown else None,
            )
        )
    return found


def parse_quartz(infos: list[dict], screens: list[Display], front_app: str | None = None) -> list[Window]:
    """Quartz's on-screen window list (front to back) as Windows: normal-layer, real-sized windows only."""
    counts: dict[str, int] = {}
    found = []
    for info in infos:
        if info.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        rect = Rect(
            round(bounds.get("X", 0)), round(bounds.get("Y", 0)), round(bounds.get("Width", 0)),
            round(bounds.get("Height", 0)),
        )  # fmt: skip
        if rect.w < 50 or rect.h < 50:
            continue
        app = str(info.get("kCGWindowOwnerName") or "?")
        counts[app] = counts.get(app, 0) + 1
        shown = display_of(rect, screens)
        found.append(
            Window(
                app=app,
                index=counts[app],
                title=str(info.get("kCGWindowName") or ""),
                rect=rect,
                app_front=app == front_app,
                display=shown.number if shown else None,
            )
        )
    return found


def _quartz_windows(screens: list[Display]) -> list[Window]:
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    return parse_quartz([dict(i) for i in infos], screens, macos.frontmost_app())


def windows(screens: list[Display] | None = None) -> list[Window]:
    """Every window of every app with a Dock icon, through System Events (needs Accessibility)."""
    return parse_windows(_system_events(*LIST_SCRIPT, timeout=20), screens or displays())


def list_all() -> tuple[list[Window], list[Display], str]:
    """(windows, displays, note). Falls back to Quartz when Accessibility is off, and says what that misses."""
    screens = displays()
    try:
        return windows(screens), screens, ""
    except ActionError as e:
        if str(e) != accessibility_hint():
            raise
    note = (
        f"(Accessibility is off for {host_app()}, so this is only what's on screen: minimized windows are missing, "
        "and moving windows won't work until it's on: System Settings → Privacy & Security → Accessibility.)"
    )
    return _quartz_windows(screens), screens, note


def describe(found: list[Window], screens: list[Display], note: str = "") -> str:
    if not found:
        return ("No windows open." + f" {note}").strip()
    lines = [f"{len(found)} windows on {len(screens)} display{'s' if len(screens) != 1 else ''}:"]
    lines += [f"- {w}" for w in found]
    lines += [f"Display {s.number}: {s.name}, {s.frame.w}x{s.frame.h}" for s in screens]
    if note:
        lines.append(note)
    return "\n".join(lines)


def find_window(found: list[Window], app: str, window: str = "") -> Window:
    """The app by exact name, else the one app whose name contains it; then its window by number or title text.
    No window named: the app's frontmost window that isn't minimized."""
    wanted = app.strip().casefold().removesuffix(".app")
    apps = sorted({w.app for w in found})
    if not wanted:
        raise ActionError("Say which app's window. Apps with windows: " + ", ".join(apps) + ".")
    exact = [a for a in apps if a.casefold() == wanted]
    partial = [a for a in apps if wanted in a.casefold()]
    chosen = exact or partial
    if not chosen:
        raise ActionError(f"No open window for '{app}'. Apps with windows: {', '.join(apps) or 'none'}.")
    if len(chosen) > 1:
        raise ActionError(f"'{app}' matches several apps: {', '.join(chosen)}. Name one exactly.")
    mine = [w for w in found if w.app == chosen[0]]
    query = window.strip()
    if not query:
        return next((w for w in mine if not w.minimized), mine[0])
    if query.isdigit():
        for w in mine:
            if w.index == int(query):
                return w
        raise ActionError(f"{chosen[0]} has {len(mine)} window(s); there's no #{query}.")
    needle = query.casefold()
    hits = [w for w in mine if needle in w.title.casefold()]
    if not hits:
        titles = "; ".join(f"#{w.index} '{w.title}'" for w in mine)
        raise ActionError(f"No {chosen[0]} window titled like '{window}'. Its windows: {titles}.")
    return hits[0]


# ---------------------------------------------------------------- geometry


def snap_rect(position: str, area: Rect, current: Rect | None = None) -> Rect:
    """Where a window goes for each snap position, inside a display's usable area."""
    x, y, w, h = area.x, area.y, area.w, area.h
    half_w, half_h = w // 2, h // 2
    spots = {
        "left": Rect(x, y, half_w, h),
        "right": Rect(x + half_w, y, w - half_w, h),
        "top": Rect(x, y, w, half_h),
        "bottom": Rect(x, y + half_h, w, h - half_h),
        "top_left": Rect(x, y, half_w, half_h),
        "top_right": Rect(x + half_w, y, w - half_w, half_h),
        "bottom_left": Rect(x, y + half_h, half_w, h - half_h),
        "bottom_right": Rect(x + half_w, y + half_h, w - half_w, h - half_h),
        "maximize": Rect(x, y, w, h),
    }
    if position in spots:
        return spots[position]
    if position == "center":
        size_w = min(current.w, w) if current else round(w * 0.7)
        size_h = min(current.h, h) if current else round(h * 0.7)
        return Rect(x + (w - size_w) // 2, y + (h - size_h) // 2, size_w, size_h)
    raise ActionError(f"Unknown position '{position}'. Use one of: {', '.join(POSITIONS)}.")


def fit(rect: Rect, area: Rect) -> Rect:
    """Shrink and shift a rectangle so it lies inside the area."""
    w, h = min(rect.w, area.w), min(rect.h, area.h)
    x = min(max(rect.x, area.x), area.x + area.w - w)
    y = min(max(rect.y, area.y), area.y + area.h - h)
    return Rect(x, y, w, h)


def carry(rect: Rect, source: Rect, target: Rect) -> Rect:
    """The same place and share of the screen on another display: a left half stays a left half."""
    sx, sy = target.w / max(source.w, 1), target.h / max(source.h, 1)
    moved = Rect(
        round(target.x + (rect.x - source.x) * sx), round(target.y + (rect.y - source.y) * sy), round(rect.w * sx),
        round(rect.h * sy),
    )  # fmt: skip
    return fit(moved, target)


# ---------------------------------------------------------------- actions

SET_FRAME_SCRIPT = (
    "on run argv",
    "set appName to item 1 of argv",
    "set idx to (item 2 of argv) as integer",
    "set nx to (item 3 of argv) as integer",
    "set ny to (item 4 of argv) as integer",
    "set nw to (item 5 of argv) as integer",
    "set nh to (item 6 of argv) as integer",
    'set lift to (item 7 of argv) is "1"',
    'tell application "System Events" to tell process appName',
    "set win to window idx",
    "try",
    'if value of attribute "AXFullScreen" of win is true then',
    'set value of attribute "AXFullScreen" of win to false',
    "delay 1",
    "end if",
    "end try",
    "try",
    'if value of attribute "AXMinimized" of win is true then set value of attribute "AXMinimized" of win to false',
    "end try",
    "set position of win to {nx, ny}",
    "set size of win to {nw, nh}",
    "set position of win to {nx, ny}",
    "if lift then",
    "try",
    'perform action "AXRaise" of win',
    "end try",
    "set frontmost to true",
    "end if",
    "set {ax, ay} to position of win",
    "set {aw, ah} to size of win",
    'return (ax as text) & "," & ay & "," & aw & "," & ah',
    "end tell",
    "end run",
)

SET_ATTRIBUTE_SCRIPT = (
    "on run argv",
    "set appName to item 1 of argv",
    "set idx to (item 2 of argv) as integer",
    "set attr to item 3 of argv",
    'set wanted to (item 4 of argv) is "1"',
    'tell application "System Events" to tell process appName',
    "set value of attribute attr of window idx to wanted",
    'if (attr is "AXFullScreen") and wanted then set frontmost to true',
    "end tell",
    "end run",
)

HIDE_SCRIPT = (
    "on run argv",
    'set wanted to (item 2 of argv) is "1"',
    'tell application "System Events" to set visible of process (item 1 of argv) to wanted',
    "end run",
)


def frame_args(win: Window, rect: Rect, lift: bool = True) -> tuple[str, ...]:
    return (win.app, str(win.index), str(rect.x), str(rect.y), str(rect.w), str(rect.h), "1" if lift else "0")


def set_frame(win: Window, rect: Rect, lift: bool = True) -> Rect:
    """Move and resize one window; returns where it really ended up (some apps refuse some sizes)."""
    raw = _system_events(*SET_FRAME_SCRIPT, argv=frame_args(win, rect, lift))
    try:
        x, y, w, h = (round(float(v)) for v in raw.split(","))
        return Rect(x, y, w, h)
    except ValueError:
        return rect


def _where(rect: Rect) -> str:
    return f"{rect.w}x{rect.h} at {rect.x},{rect.y}"


def _settled(target: Rect, actual: Rect) -> str:
    if abs(actual.w - target.w) > 20 or abs(actual.h - target.h) > 20:
        return f" (the app kept it at {actual.w}x{actual.h}: it has a size limit)"
    return ""


def _target(app: str, window: str) -> tuple[Window, list[Display]]:
    screens = displays()
    return find_window(windows(screens), app, window), screens


def snap(app: str, position: str, window: str = "", display: str = "") -> str:
    win, screens = _target(app, window)
    if position == "fullscreen":
        _system_events(*SET_ATTRIBUTE_SCRIPT, argv=(win.app, str(win.index), "AXFullScreen", "1"))
        return f"{win.app} is full screen (its own Space). Say 'maximize' to fill the screen without that."
    here = next((s for s in screens if s.number == win.display), None)
    screen = pick_display(display, screens, here)
    goal = snap_rect(position, screen.visible, win.rect)
    actual = set_frame(win, goal)
    label = {"maximize": "maximized", "center": "centred"}.get(position, f"on the {position.replace('_', ' ')}")
    return f"{win.app} {label} on display {screen.number}{_settled(goal, actual)}."


def move_resize(
    app: str,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    window: str = "",
    display: str = "",
) -> str:
    """x/y count from the top-left of the display's usable area (below the menu bar)."""
    if x is None and y is None and width is None and height is None:
        raise ActionError("Give at least one of x, y, width, height.")
    win, screens = _target(app, window)
    here = next((s for s in screens if s.number == win.display), None)
    screen = pick_display(display, screens, here)
    area = screen.visible
    rect = win.rect
    goal = fit(
        Rect(
            area.x + x if x is not None else rect.x,
            area.y + y if y is not None else rect.y,
            width if width is not None else rect.w,
            height if height is not None else rect.h,
        ),
        area,
    )
    actual = set_frame(win, goal)
    spot = Rect(actual.x - area.x, actual.y - area.y, actual.w, actual.h)
    return f"{win.app} is now {_where(spot)} on display {screen.number}{_settled(goal, actual)}."


def to_display(app: str, display: str = "next", window: str = "") -> str:
    win, screens = _target(app, window)
    here = next((s for s in screens if s.number == win.display), screens[0])
    screen = pick_display(display or "next", screens, here)
    if screen is here:
        return f"{win.app} is already on display {screen.number}."
    goal = carry(win.rect, here.visible, screen.visible)
    set_frame(win, goal)
    return f"Moved {win.app} to display {screen.number} ({screen.name})."


def minimize(app: str, window: str = "", minimized: bool = True) -> str:
    screens = displays()
    found = windows(screens)
    win = find_window(found, app, window)
    if not minimized and not window:  # restoring: the minimized one, not the one already showing
        win = next((w for w in found if w.app == win.app and w.minimized), win)
    _system_events(*SET_ATTRIBUTE_SCRIPT, argv=(win.app, str(win.index), "AXMinimized", "1" if minimized else "0"))
    return f"{'Minimized' if minimized else 'Restored'} {win.app}{f' ({win.title})' if win.title else ''}."


def hide(app: str, hidden: bool = True) -> str:
    target = macos._find_app(app)
    _system_events(*HIDE_SCRIPT, argv=(target.name, "0" if hidden else "1"))
    return f"{'Hid' if hidden else 'Showing'} {target.name}."


def side_by_side(left_app: str, right_app: str, display: str = "") -> str:
    screens = displays()
    found = windows(screens)
    left = find_window(found, left_app)
    right = find_window(found, right_app)
    if left.app == right.app and left.index == right.index:
        raise ActionError("That's the same window twice: name two different apps.")
    here = next((s for s in screens if s.number == left.display), None)
    screen = pick_display(display, screens, here)
    set_frame(right, snap_rect("right", screen.visible))
    set_frame(left, snap_rect("left", screen.visible))
    return f"{left.app} on the left, {right.app} on the right, on display {screen.number}."
