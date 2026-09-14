import pytest

from sommus.nodes.laptop import macos

PMSET_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=21037155)\t59%; discharging; 3:32 remaining present: true
"""

PMSET_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=21037155)\t80%; charging; (no estimate) present: true
"""

LSAPPINFO = """ 1) "loginwindow" ASN:0x0-0x2002:
    bundleID="com.apple.loginwindow"
    pid = 381 type="UIElement" flavor=3 Version="3035"

 2) "Finder" ASN:0x0-0x1001:
    bundleID="com.apple.finder"
    pid = 612 type="Foreground" flavor=3 Version="15.0"

 3) "Google Chrome" ASN:0x0-0x1d01d:
    bundleID="com.google.Chrome"
    pid = 901 type="Foreground" flavor=3 Version="140.0"
"""


def test_parse_battery_discharging():
    assert macos.parse_battery(PMSET_BATTERY) == "59%, discharging, 3:32 remaining, on Battery Power"


def test_parse_battery_charging_without_estimate():
    assert macos.parse_battery(PMSET_CHARGING) == "80%, charging, on AC Power"


def test_parse_volume():
    state = macos.parse_volume("output volume:35, input volume:27, alert volume:100, output muted:true")
    assert state == macos.VolumeState(output=35, muted=True)


def test_parse_volume_without_software_control():
    state = macos.parse_volume("output volume:missing value, input volume:27, alert volume:100, output muted:false")
    assert state.output is None


def test_parse_lsappinfo_keeps_only_foreground_apps():
    assert macos.parse_lsappinfo(LSAPPINFO) == [
        macos.RunningApp("Finder", 612),
        macos.RunningApp("Google Chrome", 901),
    ]


@pytest.mark.parametrize("level", [-1, 101])
def test_set_volume_rejects_out_of_range(level):
    with pytest.raises(macos.ActionError):
        macos.set_volume(level)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://", "spotify:track:1"])
def test_open_url_rejects_non_web_urls(url, monkeypatch):
    monkeypatch.setattr(macos, "_run", lambda args: pytest.fail("should not run"))
    with pytest.raises(macos.ActionError):
        macos.open_url(url)


def test_find_app_matching(monkeypatch):
    monkeypatch.setattr(macos, "running_apps", lambda: macos.parse_lsappinfo(LSAPPINFO))
    assert macos._find_app("chrome").name == "Google Chrome"
    assert macos._find_app("FINDER.app").name == "Finder"
    with pytest.raises(macos.ActionError, match="No running app"):
        macos._find_app("Spotify")
