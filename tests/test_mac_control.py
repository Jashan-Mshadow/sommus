"""Window management, Terminal control and Chrome tabs by voice: parsing, the AppleScript that would run, and the
refusals. Nothing here runs osascript: every runner is a fake that records what it was asked."""

import os
import signal

import pytest

from sommus import config as config_module
from sommus.brain import fastpath
from sommus.brain.loop import OUTWARD_TOOLS, UNTRUSTED_TOOLS
from sommus.interfaces import live
from sommus.nodes.laptop import browser, macos, terminal, windows
from sommus.nodes.laptop.macos import ActionError

# Two displays as NSScreen reports them: the laptop (main, menu bar, Dock at the bottom) and a 1920x1080 monitor
# to its right, its bottom 180 points lower than the laptop's (so its top is at y=0 too... in flipped terms: below).
SCREENS_RAW = "0,0,1440,900,0,58,1440,817,Built-in Retina Display\n1440,-180,1920,1080,1440,-180,1920,1055,DELL U2720Q"


def screens():
    return windows.parse_displays(SCREENS_RAW)


def record(app, index, title, x, y, w, h, mini=False, front=False):
    f = windows.FIELD
    return f"{app}{f}{index}{f}{title}{f}{x}{f}{y}{f}{w}{f}{h}{f}{str(mini).lower()}{f}{str(front).lower()}"


WINDOWS_RAW = windows.RECORD.join(
    [
        record("Google Chrome", 1, "YouTube", 100, 100, 800, 600, front=True),
        record("Google Chrome", 2, "Gmail", 50, 60, 700, 500, mini=True),
        record("Notes", 1, "Groceries", 1600, 200, 600, 400),
        record("Terminal", 1, "sommus — sommus-live.command — 80x24", 0, 25, 700, 450),
    ]
)


# ---------------------------------------------------------------- displays and geometry


def test_displays_flip_to_top_left_coordinates_and_drop_the_menu_bar_and_dock():
    laptop, monitor = screens()
    assert (laptop.number, laptop.built_in) == (1, True)
    assert laptop.frame == windows.Rect(0, 0, 1440, 900)
    assert laptop.visible == windows.Rect(0, 25, 1440, 817)  # 25 menu bar on top, 58 Dock below
    assert monitor.frame == windows.Rect(1440, 0, 1920, 1080)
    assert monitor.visible == windows.Rect(1440, 25, 1920, 1055)
    assert not monitor.built_in


@pytest.mark.parametrize(
    ("position", "rect"),
    [
        ("left", windows.Rect(0, 25, 720, 817)),
        ("right", windows.Rect(720, 25, 720, 817)),
        ("maximize", windows.Rect(0, 25, 1440, 817)),
        ("top", windows.Rect(0, 25, 1440, 408)),
        ("bottom_right", windows.Rect(720, 433, 720, 409)),
        ("center", windows.Rect(320, 133, 800, 600)),
    ],
)
def test_snap_positions_fill_the_usable_area(position, rect):
    laptop = screens()[0]
    assert windows.snap_rect(position, laptop.visible, windows.Rect(5, 5, 800, 600)) == rect


def test_an_unknown_position_is_a_readable_error():
    with pytest.raises(ActionError, match="Use one of"):
        windows.snap_rect("diagonal", screens()[0].visible)


def test_a_window_carried_to_another_display_keeps_its_share_of_the_screen():
    laptop, monitor = screens()
    left_half = windows.snap_rect("left", laptop.visible)
    moved = windows.carry(left_half, laptop.visible, monitor.visible)
    assert moved == windows.Rect(1440, 25, 960, 1055)


def test_which_display_a_window_is_on_and_how_displays_are_named():
    laptop, monitor = screens()
    assert windows.display_of(windows.Rect(1600, 200, 600, 400), [laptop, monitor]) is monitor
    assert windows.pick_display("next", [laptop, monitor], laptop) is monitor
    assert windows.pick_display("other", [laptop, monitor], monitor) is laptop
    assert windows.pick_display("built-in", [laptop, monitor]) is laptop
    assert windows.pick_display("external", [laptop, monitor]) is monitor
    assert windows.pick_display("2", [laptop, monitor]) is monitor
    assert windows.pick_display("dell", [laptop, monitor]) is monitor
    with pytest.raises(ActionError, match="Only one display"):
        windows.pick_display("next", [laptop], laptop)


# ---------------------------------------------------------------- listing and finding


def test_windows_parse_with_minimized_state_and_display():
    found = windows.parse_windows(WINDOWS_RAW, screens())
    assert [(w.app, w.index, w.minimized, w.display) for w in found] == [
        ("Google Chrome", 1, False, 1),
        ("Google Chrome", 2, True, 1),
        ("Notes", 1, False, 2),
        ("Terminal", 1, False, 1),
    ]
    text = windows.describe(found, screens())
    assert "4 windows on 2 displays" in text and "minimized" in text and "[front app]" in text


def test_finding_a_window_by_app_part_of_a_name_title_or_number():
    found = windows.parse_windows(WINDOWS_RAW, screens())
    assert windows.find_window(found, "chrome").index == 1  # front, not minimized
    assert windows.find_window(found, "Chrome", "gmail").index == 2
    assert windows.find_window(found, "google chrome", "2").title == "Gmail"
    with pytest.raises(ActionError, match="No open window for 'Spotify'"):
        windows.find_window(found, "Spotify")
    with pytest.raises(ActionError, match="no #5"):
        windows.find_window(found, "Notes", "5")


def test_quartz_fallback_keeps_normal_windows_only():
    infos = [
        {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Notes", "kCGWindowName": "x",
         "kCGWindowBounds": {"X": 10, "Y": 30, "Width": 500, "Height": 400}},
        {"kCGWindowLayer": 25, "kCGWindowOwnerName": "Control Center", "kCGWindowBounds": {"X": 0, "Y": 0,
         "Width": 30, "Height": 24}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerName": "Dock", "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 10,
         "Height": 10}},
    ]  # fmt: skip
    found = windows.parse_quartz(infos, screens(), "Notes")
    assert [(w.app, w.rect.w, w.app_front) for w in found] == [("Notes", 500, True)]


def test_without_accessibility_the_listing_falls_back_and_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(windows, "_jxa", lambda script: SCREENS_RAW)

    def blocked(*lines, argv=(), timeout=10):
        raise ActionError("execution error: System Events got an error: osascript is not allowed assistive access.")

    monkeypatch.setattr(macos, "_osascript", blocked)
    monkeypatch.setattr(windows, "_quartz_windows", lambda s: windows.parse_windows(WINDOWS_RAW, s)[:1])
    found, _, note = windows.list_all()
    assert len(found) == 1 and "Accessibility" in note


def test_moving_without_accessibility_says_which_permission_to_turn_on(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.setattr(windows, "_jxa", lambda script: SCREENS_RAW)

    def blocked(*lines, argv=(), timeout=10):
        raise ActionError("27:80: execution error: osascript is not allowed assistive access. (-25211)")

    monkeypatch.setattr(macos, "_osascript", blocked)
    with pytest.raises(ActionError) as caught:
        windows.snap("chrome", "left")
    message = str(caught.value)
    assert "Accessibility" in message and "Terminal" in message and "-25211" not in message


def test_automation_refusal_is_a_plain_sentence_too():
    error = windows.plain(ActionError("Not authorized to send Apple events to System Events. (-1743)"))
    assert "Automation" in str(error) and "-1743" not in str(error)


# ---------------------------------------------------------------- actions, through a fake runner


class FakeSystemEvents:
    """Answers the listing script with WINDOWS_RAW and records every other script with its argv."""

    def __init__(self):
        self.calls = []

    def __call__(self, *lines, argv=(), timeout=10):
        if lines == windows.LIST_SCRIPT:
            return WINDOWS_RAW
        self.calls.append((lines, argv))
        if lines == windows.SET_FRAME_SCRIPT:
            return ",".join(argv[2:6])
        return ""


@pytest.fixture
def system_events(monkeypatch):
    fake = FakeSystemEvents()
    monkeypatch.setattr(macos, "_osascript", fake)
    monkeypatch.setattr(windows, "_jxa", lambda script: SCREENS_RAW)
    return fake


def test_snap_sends_the_window_and_frame_as_arguments_never_inside_the_script(system_events):
    assert windows.snap("chrome", "left") == "Google Chrome on the left on display 1."
    lines, argv = system_events.calls[0]
    assert lines == windows.SET_FRAME_SCRIPT
    assert argv == ("Google Chrome", "1", "0", "25", "720", "817", "1")
    assert not any("Google Chrome" in line for line in lines)


def test_maximize_on_another_display_and_fullscreen(system_events):
    windows.snap("chrome", "maximize", display="external")
    assert system_events.calls[-1][1][2:6] == ("1440", "25", "1920", "1055")
    assert "full screen" in windows.snap("notes", "fullscreen")
    assert system_events.calls[-1] == (windows.SET_ATTRIBUTE_SCRIPT, ("Notes", "1", "AXFullScreen", "1"))


def test_a_size_the_app_refuses_is_reported(monkeypatch, system_events):
    def stubborn(*lines, argv=(), timeout=10):
        if lines == windows.LIST_SCRIPT:
            return WINDOWS_RAW
        return "0,25,500,400"

    monkeypatch.setattr(macos, "_osascript", stubborn)
    assert "size limit" in windows.snap("chrome", "maximize")


def test_move_to_the_other_display_and_side_by_side(system_events):
    assert windows.to_display("notes") == "Moved Notes to display 1 (Built-in Retina Display)."
    windows.side_by_side("chrome", "notes")
    right, left = system_events.calls[-2], system_events.calls[-1]
    assert right[1][:6] == ("Notes", "1", "720", "25", "720", "817")
    assert left[1][:6] == ("Google Chrome", "1", "0", "25", "720", "817")


def test_move_resize_counts_from_the_usable_area_and_stays_on_screen(system_events):
    windows.move_resize("chrome", x=100, y=0, width=5000)
    assert system_events.calls[-1][1][2:6] == ("0", "25", "1440", "600")  # 5000 wide doesn't fit: shrunk
    with pytest.raises(ActionError, match="at least one"):
        windows.move_resize("chrome")


def test_restoring_a_minimized_window_picks_the_minimized_one(system_events):
    assert windows.minimize("chrome", minimized=False) == "Restored Google Chrome (Gmail)."
    assert system_events.calls[-1][1] == ("Google Chrome", "2", "AXMinimized", "0")


def test_hide_app_uses_the_running_name(monkeypatch, system_events):
    monkeypatch.setattr(macos, "running_apps", lambda: [macos.RunningApp("Discord", 5)])
    assert windows.hide("discord") == "Hid Discord."
    assert system_events.calls[-1] == (windows.HIDE_SCRIPT, ("Discord", "0"))


# ---------------------------------------------------------------- Terminal


def tab_record(window, tab, wid, title, busy, procs, tty, selected=True, front=False, custom=""):
    f = terminal.FIELD
    fields = [window, tab, wid, title, custom, str(busy).lower(), procs, tty, str(selected).lower(), str(front).lower()]
    return f.join(str(x) for x in fields)


TABS_RAW = terminal.RECORD.join(
    [
        tab_record(1, 1, 11, "sommus — sommus-live.command — 80×24", True, "login,bash,python3.12", "/dev/ttys002",
                   front=True),
        tab_record(2, 1, 22, "web — npm run dev", True, "login,-zsh,npm,node", "/dev/ttys004"),
        tab_record(3, 1, 33, "zsh", False, "login,-zsh", "/dev/ttys006", selected=False),
        tab_record(3, 2, 33, "zsh", False, "login,-zsh", "/dev/ttys007"),
        tab_record(4, 1, 44, "zsh", False, "login,-zsh", "/dev/ttys009"),
    ]
)  # fmt: skip


class FakeTerminal:
    def __init__(self, raw=TABS_RAW):
        self.raw = raw
        self.calls = []

    def __call__(self, *lines, argv=(), timeout=10):
        if lines == terminal.LIST_SCRIPT:
            return self.raw
        self.calls.append((lines, argv))
        if lines == terminal.NEW_WINDOW_SCRIPT:
            return "/dev/ttys010"
        if lines == terminal.HISTORY_SCRIPT:
            return "\n".join(f"line {i}" for i in range(1, 101)) + "\n\n\n"
        return ""


@pytest.fixture
def fake_terminal(monkeypatch):
    fake = FakeTerminal()
    monkeypatch.setattr(macos, "_osascript", fake)
    monkeypatch.setattr(terminal, "protected_ttys", lambda: set())
    return fake


def test_tabs_parse_and_the_live_window_is_marked_by_its_title():
    tabs = terminal.parse_tabs(TABS_RAW)
    assert [t.number for t in tabs] == [1, 2, 3, 4, 5]
    assert [t.protected for t in tabs] == [True, False, False, False, False]
    assert tabs[1].running == ["npm", "node"] and tabs[1].busy
    text = terminal.describe(tabs)
    assert "5 Terminal tab(s), 2 busy" in text and "hands off" in text


def test_a_tab_on_sommus_tty_guards_its_whole_window_whatever_the_title():
    raw = terminal.RECORD.join(
        [tab_record(1, 1, 11, "zsh", False, "login,-zsh", "/dev/ttys001", selected=False),
         tab_record(1, 2, 11, "python", True, "python3", "/dev/ttys002")]
    )  # fmt: skip
    tabs = terminal.parse_tabs(raw, {"/dev/ttys002"})
    assert all(t.protected for t in tabs)


def test_finding_tabs_by_number_label_current_and_text():
    tabs = terminal.parse_tabs(TABS_RAW)
    assert terminal.find_tab(tabs, "2").tty == "/dev/ttys004"
    assert terminal.find_tab(tabs, "3.2").tty == "/dev/ttys007"
    assert terminal.find_tab(tabs, "current").number == 1  # the front window's tab (Sommus's, refused later)
    assert terminal.find_tab(tabs, "npm").number == 2
    with pytest.raises(ActionError, match="matches 4 tabs; pick one by number"):
        terminal.find_tab(tabs, "zsh")


def test_nothing_is_ever_sent_to_the_window_sommus_runs_in(fake_terminal):
    for action in (
        lambda: terminal.send("1", text="ls"),
        lambda: terminal.send("sommus", keys="ctrl+c"),
        lambda: terminal.close("1"),
        lambda: terminal.read("1"),
    ):
        with pytest.raises(ActionError, match="Sommus itself runs in"):
            action()
    assert fake_terminal.calls == []


def test_a_busy_tab_is_never_closed(fake_terminal):
    with pytest.raises(ActionError, match="still running npm, node"):
        terminal.close("2")
    assert fake_terminal.calls == []


def test_a_finished_tab_alone_in_its_window_closes_the_window(fake_terminal):
    assert terminal.close("5") == "Closed tab 5 (its window)."
    assert fake_terminal.calls == [(terminal.CLOSE_WINDOW_SCRIPT, ("44",))]


def test_an_idle_tab_among_others_is_closed_by_ending_its_shell(monkeypatch, fake_terminal):
    monkeypatch.setattr(terminal.time, "sleep", lambda s: None)

    def after_exit(*lines, argv=(), timeout=10):
        if lines == terminal.LIST_SCRIPT:
            exited = any(c[1][2:] == ("exit",) for c in fake_terminal.calls)
            return TABS_RAW if not exited else TABS_RAW.replace(terminal.RECORD + tab_record(3, 2, 33, "zsh", False,
                                                          "login,-zsh", "/dev/ttys007"), "")  # fmt: skip
        fake_terminal.calls.append((lines, argv))
        return ""

    monkeypatch.setattr(macos, "_osascript", after_exit)
    assert terminal.close("4") == "Closed tab 4."
    assert fake_terminal.calls == [(terminal.DO_SCRIPT, ("33", "2", "exit"))]


def test_text_goes_to_the_tab_as_an_argument(fake_terminal):
    assert terminal.send("2", text="git status") == "Sent 'git status' to tab 2 and pressed Return."
    lines, argv = fake_terminal.calls[0]
    assert lines == terminal.DO_SCRIPT and argv == ("22", "1", "git status")
    assert not any("git status" in line for line in lines)


@pytest.mark.parametrize("bad", ["sudo rm -rf /tmp/x", "ls\nrm x"])
def test_sudo_and_several_lines_are_refused(fake_terminal, bad):
    with pytest.raises(ActionError):
        terminal.send("2", text=bad)
    assert fake_terminal.calls == []


def test_ctrl_c_is_sent_as_sigint_to_the_foreground_processes(monkeypatch, fake_terminal):
    killed = []
    monkeypatch.setattr(macos, "_run", lambda args, timeout=10: " 501 Ss   \n 777 S+   \n 778 R+   ")
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert terminal.send("2", keys="Control-C") == "Sent ctrl+c to tab 2 (2 processes)."
    assert killed == [(777, signal.SIGINT), (778, signal.SIGINT)]
    assert "nothing for ctrl+c to stop" in terminal.send("3.2", keys="^C")


def test_other_keys_check_the_tab_is_in_front_before_pressing(monkeypatch, fake_terminal):
    pressed = []
    monkeypatch.setattr(terminal.time, "sleep", lambda s: None)
    monkeypatch.setattr(macos, "press_keys", lambda combo: pressed.append(combo) or combo)
    monkeypatch.setattr(macos, "frontmost_app", lambda: "Notes")  # focusing didn't work
    with pytest.raises(ActionError, match="nothing was typed"):
        terminal.send("2", keys="q")
    assert pressed == []


def test_return_alone_needs_no_keystroke(fake_terminal):
    assert terminal.send("2", keys="enter") == "Pressed Return in tab 2."
    assert fake_terminal.calls[-1] == (terminal.DO_SCRIPT, ("22", "1", ""))


def test_reading_gives_the_last_lines_only(fake_terminal):
    tab, text = terminal.read("2", lines=3)
    assert text == "line 98\nline 99\nline 100" and tab.number == 2


def test_opening_a_window_quotes_the_folder(tmp_path, fake_terminal):
    folder = tmp_path / "my project"
    folder.mkdir()
    said = terminal.open_tab("npm run dev", str(folder))
    assert "running `npm run dev`" in said
    lines, argv = fake_terminal.calls[-1]
    assert lines == terminal.NEW_WINDOW_SCRIPT and argv == (f"cd '{folder}' && npm run dev",)
    with pytest.raises(ActionError, match="isn't a folder"):
        terminal.open_tab("ls", str(tmp_path / "missing"))


@pytest.mark.parametrize(("said", "combo"), [("Ctrl+C", "ctrl+c"), ("control c", "ctrl+c"), ("^D", "ctrl+d"),
                                             ("Enter", "return"), ("cmd+shift+t", "cmd+shift+t")])  # fmt: skip
def test_key_names_are_normalised(said, combo):
    assert terminal.normal_keys(said) == combo


# ---------------------------------------------------------------- tiers, PIN, outside content


def test_terminal_and_window_listing_wait_for_the_pin_like_run_shell():
    pin_tools = set(config_module.load().pin_tools)
    assert {"run_shell", "send_to_terminal", "open_terminal", "close_terminal_tab", "read_terminal",
            "list_terminal_tabs", "list_windows"} <= pin_tools  # fmt: skip
    assert not {"snap_window", "move_window", "close_browser_tabs", "focus_browser_tab"} & pin_tools


def test_terminal_contents_are_outside_content_and_typing_needs_his_own_words():
    assert "read_terminal" in UNTRUSTED_TOOLS
    assert OUTWARD_TOOLS["send_to_terminal"] >= OUTWARD_TOOLS["run_shell"]
    assert "open_terminal" in OUTWARD_TOOLS


def test_gemini_gets_tab_and_window_tools_but_not_terminal_or_listings():
    names = {"close_browser_tabs", "focus_browser_tab", "snap_window", "arrange_side_by_side", "send_to_terminal",
             "list_windows", "list_browser_tabs", "read_terminal"}  # fmt: skip
    tools = [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in sorted(names)]
    got = {d["name"] for d in live.declarations(tools)}
    assert got == {"close_browser_tabs", "focus_browser_tab", "snap_window", "arrange_side_by_side", "ask_sommus"}
    assert "close_browser_tabs" in live.instructions("Sommus", "Jashan")


# ---------------------------------------------------------------- Chrome tabs

CHROME = [
    browser.Tab(1, 1, "Inbox - Gmail", "https://mail.google.com", False),
    browser.Tab(1, 2, "lofi - YouTube", "https://www.youtube.com/watch?v=1", True),
    browser.Tab(2, 1, "cats - YouTube", "https://www.youtube.com/watch?v=2", True),
    browser.Tab(2, 2, "LEARN", "https://learn.uwaterloo.ca", False),
]


def test_this_tab_is_the_one_in_front_and_all_matching_closes_every_youtube_tab(monkeypatch):
    scripts = []
    monkeypatch.setattr(browser, "list_tabs", lambda: CHROME)
    monkeypatch.setattr(browser, "_script", lambda body, timeout=20: scripts.append(body) or "")
    assert [t.title for t in browser.close_tabs(["current"])] == ["lofi - YouTube"]
    assert [t.title for t in browser.close_tabs(["youtube"])] == ["lofi - YouTube"]
    assert [t.title for t in browser.close_tabs(["YouTube"], all_matching=True)] == ["lofi - YouTube",
                                                                                      "cats - YouTube"]  # fmt: skip
    assert scripts[-2:] == ["close tab 1 of window 2", "close tab 2 of window 1"]


def test_a_tab_that_isnt_open_says_so_without_reading_every_title(monkeypatch):
    monkeypatch.setattr(browser, "list_tabs", lambda: CHROME)
    with pytest.raises(ActionError) as caught:
        browser.close_tabs(["netflix"])
    assert "Gmail" not in str(caught.value) and "4 tabs are open" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("close this tab", "close_browser_tabs", {"tabs": ["current"]}),
        ("close that tab", "close_browser_tabs", {"tabs": ["current"]}),
        ("close the tab", "close_browser_tabs", {"tabs": ["current"]}),
        ("Sommus, can you close the current tab please", "close_browser_tabs", {"tabs": ["current"]}),
        ("close the chrome tab", "close_browser_tabs", {"tabs": ["current"]}),
        ("close the youtube tab", "close_browser_tabs", {"tabs": ["youtube"], "all_matching": False}),
        ("close my LEARN tab", "close_browser_tabs", {"tabs": ["learn"], "all_matching": False}),
        ("close all youtube tabs", "close_browser_tabs", {"tabs": ["youtube"], "all_matching": True}),
        ("close all of my youtube tabs", "close_browser_tabs", {"tabs": ["youtube"], "all_matching": True}),
        ("close my youtube tabs", "close_browser_tabs", {"tabs": ["youtube"], "all_matching": True}),
        ("shut the google docs tab", "close_browser_tabs", {"tabs": ["google docs"], "all_matching": False}),
        ("switch to my gmail tab", "focus_browser_tab", {"tab": "gmail"}),
        ("go back to the youtube tab", "focus_browser_tab", {"tab": "youtube"}),
        ("show me the learn tab", "focus_browser_tab", {"tab": "learn"}),
        ("open the gmail tab", "focus_browser_tab", {"tab": "gmail"}),
    ],
)
def test_tab_commands_skip_the_model(text, tool, args):
    found = fastpath.match(text, apps=lambda: {"notes": "Notes", "chrome": "Google Chrome"})
    assert found is not None, text
    assert (found.tool, found.args) == (tool, args)


@pytest.mark.parametrize(
    "text",
    [
        "close all tabs",  # too much at once
        "close all the tabs except gmail",
        "close the other tabs",
        "close the tab and open gmail",
        "open a new tab",
        "open youtube in a new tab",
        "go to the next tab",
        "switch tabs",
        "switch to this tab",
        "close tab 3",
        "close the youtube tab I opened yesterday",
        "why did you close my youtube tab",
        "did you close the youtube tab",
        "close these two tabs",
        "close chrome",  # quitting the app is the app intent's job, not a tab
        "close the window",
        "close the tab with the lecture video in it",
    ],
)
def test_tab_commands_leave_the_rest_to_the_model(text):
    found = fastpath.match(text, apps=lambda: {"notes": "Notes", "chrome": "Google Chrome"})
    assert found is None or found.intent not in {"tab_close", "tab_focus"}, text


def test_tab_replies_are_short_and_never_read_titles():
    found = fastpath.match("close all youtube tabs")
    assert found.phrase("Closed 3 tabs: a; b; c") == "Closed 3 Youtube tabs."
    assert fastpath.match("close the youtube tab").phrase("Closed 1 tab: lofi") == "Closed the Youtube tab."
    assert fastpath.match("close this tab").phrase("Closed 1 tab: secret title") == "Tab closed."
