"""Everyday commands answered without the model: $0 and instant.

Not a list of exact sentences — people never say the same thing twice. Each request is
reduced to words; an intent claims it only when *every* remaining word belongs to that
intent's vocabulary. "lower brightness by 12%", "make the screen a bit dimmer" and
"brightness down 12" all land on one intent, while "set brightness to 20 and open Spotify"
("and open spotify" left over) or "why is my battery draining" ("why draining") go to the
model. A false match does the wrong thing; a missed one only costs what it did before,
so the rule leans hard towards missing.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Words that carry no intent of their own.
FILLER = {
    "please", "pls", "can", "could", "would", "will", "you", "u", "the", "my", "a", "an", "it", "its",
    "sommus", "somis", "sommis", "hey", "yo", "ok", "okay", "just", "set", "turn", "make", "put", "change", "get",
    "give", "tell", "me", "to", "at", "by", "percent", "so", "is", "whats", "what", "of", "level", "current",
    "currently", "now", "right", "laptop", "mac", "macbook", "computer", "for", "be", "how", "much",
    "bit", "little", "slightly", "lot", "some", "abit", "thanks", "thank", "up", "down",
}  # fmt: skip
UP = {"up", "increase", "raise", "higher", "louder", "brighter", "boost", "more", "loud"}
DOWN = {"down", "decrease", "lower", "reduce", "quieter", "softer", "dim", "dimmer", "darker", "less", "quiet"}
MAX = {"max", "maximum", "full"}
MIN = {"min", "minimum", "zero"}
STEP_SMALL, STEP_DEFAULT, STEP_BIG = 5, 10, 25
WEATHER_TRIGGERS = {"weather", "temperature", "temp", "rain", "raining", "forecast", "cold", "hot", "warm", "snow",
                    "snowing", "umbrella"}  # fmt: skip

VOCAB = {
    "battery": {"battery", "charge", "charged", "charging", "percentage", "left", "life", "remaining", "low"},
    "time": {"time", "clock"},
    "date": {"date", "day", "today", "todays"},
    "brightness": {"brightness", "bright", "brighter", "dim", "dimmer", "darker", "screen", "display"}
    | UP
    | DOWN
    | MAX
    | MIN,
    "volume": {"volume", "sound", "audio", "louder", "quieter", "softer", "loud", "quiet"} | UP | DOWN | MAX | MIN,
    "mute": {"mute", "sound", "audio", "volume"},
    "unmute": {"unmute", "sound", "audio", "volume"},
    "play_pause": {"pause", "play", "resume", "stop", "music", "song", "spotify", "playback"},
    "next": {"skip", "next", "song", "track", "this"},
    "previous": {"previous", "back", "go", "last", "song", "track"},
    "lock": {"lock", "screen"},
    "screen_off": {"off", "screen", "display", "sleep", "monitor"},
    "weather": WEATHER_TRIGGERS
    | {"outside", "today", "tomorrow", "degrees", "like", "going", "need", "i", "do", "an", "does", "feel"},

    "holiday": {"holiday", "holidays", "next", "upcoming", "coming", "stat", "statutory", "long", "weekend", "day",
                "off", "when", "does", "fall", "on", "this", "year", "are", "there", "any"},
}  # fmt: skip

# Words that make a sentence more than one simple command.
STOP_WORDS = {"and", "then", "also", "after", "before", "if", "when", "why", "dont", "not", "never", "no", "or"}
# Said after the place ("time in Delhi right now"), so not part of its name.
AFTER_PLACE = {"right", "now", "rn", "today", "tomorrow", "currently", "please", "pls", "tonight", "outside"}


@dataclass(frozen=True)
class Match:
    intent: str
    tool: str | None = None  # tool to run; None for answers computed right here
    args: dict[str, Any] = field(default_factory=dict)
    read_tool: str | None = None  # for relative changes: read the current level first
    delta: int = 0
    local: Callable[[], str] | None = field(default=None, compare=False)
    phrase: Callable[[str], str] | None = field(default=None, compare=False)


def words(text: str) -> list[str]:
    text = text.lower().replace("what's", "whats").replace("it's", "its").replace("don't", "dont")
    return re.findall(r"[a-z]+|\d{1,4}", text)


def _number(tokens: list[str]) -> int | None:
    numbers = [int(t) for t in tokens if t.isdigit()]
    return numbers[0] if len(numbers) == 1 else None


def _claims(intent: str, tokens: list[str]) -> bool:
    content = [t for t in tokens if not t.isdigit() and t not in FILLER]
    return bool(content) and all(t in VOCAB[intent] for t in content)


def _level_change(intent: str, tokens: list[str], raw: str) -> Match | None:
    """brightness/volume: absolute ("to 40"), relative ("by 12", "a bit"), or just read it."""
    if intent == "brightness":
        set_tool, read_tool = "set_brightness", "get_brightness"
    else:
        set_tool, read_tool = "set_volume", "get_volume"
    number = _number(tokens)
    if number is not None and number > 100:
        return None
    going_up = any(t in UP for t in tokens)
    going_down = any(t in DOWN for t in tokens)
    if going_up and going_down:
        return None
    if any(t in MAX for t in tokens):
        return Match(intent, set_tool, {"level": 100})
    if any(t in MIN for t in tokens):
        return Match(intent, set_tool, {"level": 0})
    if intent == "brightness" and not ({"brightness", "bright", "brighter", "dim", "dimmer", "darker"} & set(tokens)):
        return None  # "screen" alone isn't about brightness
    relative = re.search(r"\bby\s+\d", raw) or ((going_up or going_down) and not re.search(r"\b(to|at)\s+\d", raw))
    if relative:
        if not (going_up or going_down):
            return None  # "brightness by 12" — which way?
        if number is not None:
            step = number
        elif "slightly" in tokens:
            step = STEP_SMALL
        elif "lot" in tokens:
            step = STEP_BIG
        else:
            step = STEP_DEFAULT
        return Match(intent, set_tool, read_tool=read_tool, delta=step if going_up else -step)
    if number is not None:
        return Match(intent, set_tool, {"level": number})
    return Match(intent, read_tool)  # "what's the volume"


def split_place(tokens: list[str]) -> tuple[list[str], str | None]:
    """'what time is it in new delhi right now' -> (['what', 'time', 'is', 'it', 'right', 'now'], 'new delhi')"""
    if "in" not in tokens:
        return tokens, None
    at = len(tokens) - 1 - tokens[::-1].index("in")
    after = tokens[at + 1 :]
    place = [t for t in after if t not in AFTER_PLACE and t not in {"the"}]
    if not place or len(place) > 3 or any(t.isdigit() for t in place):
        return tokens, None
    return tokens[:at] + [t for t in after if t in AFTER_PLACE], " ".join(place)


def match(
    text: str,
    weather: Callable[[list[str]], str] | None = None,
    places: Callable[[str], Any] | None = None,
) -> Match | None:
    raw = " ".join(text.strip().lower().split())
    tokens = words(raw)
    present = set(tokens)
    # "when is Diwali" is a question, not "when X happens, do Y" — only a holiday intent can claim it.
    stopped = present & STOP_WORDS - ({"when"} if tokens[:1] == ["when"] else set())
    if not tokens or len(tokens) > 12 or stopped:
        return None

    if _claims("screen_off", tokens) and present & {"off", "sleep"} and present & {"screen", "display", "monitor"}:
        return Match("screen_off", "sleep_display", phrase=lambda _: "Screen's off.")
    if _claims("lock", tokens) and "lock" in present:
        return Match("lock", "lock_screen", phrase=lambda _: "Locked.")
    if _claims("unmute", tokens) and "unmute" in present:
        return Match("unmute", "set_mute", {"muted": False}, phrase=lambda _: "Sound's back on.")
    if _claims("mute", tokens) and "mute" in present:
        return Match("mute", "set_mute", {"muted": True}, phrase=lambda _: "Muted.")
    if _claims("next", tokens) and present & {"skip", "next"}:
        return Match("next", "media_control", {"action": "next"}, phrase=lambda _: "Skipped.")
    if _claims("previous", tokens) and present & {"previous", "back", "last"}:
        return Match("previous", "media_control", {"action": "previous"}, phrase=lambda _: "Going back.")
    if _claims("play_pause", tokens) and present & {"pause", "play", "resume", "stop"}:
        return Match("play_pause", "media_control", {"action": "play_pause"}, phrase=lambda _: "Done.")
    if _claims("battery", tokens) and "battery" in present:
        return Match("battery", "get_battery", phrase=say_battery)
    if _claims("time", tokens) and "time" in present:
        return Match("time", local=lambda: f"It's {datetime.now():%-I:%M %p}.")
    rest, place = split_place(tokens)
    if place and places:
        if _claims("time", rest) and "time" in rest:
            return Match("time_in", local=lambda: time_in(places(place)))
        if weather and _claims("weather", rest) and set(rest) & WEATHER_TRIGGERS:
            return Match("weather_in", local=lambda: weather(rest, places(place)))
    if _claims("holiday", tokens) and present & {"holiday", "holidays"} and present & {"next", "upcoming", "coming"}:
        return Match("holiday", local=next_holidays)
    if tokens[:2] == ["when", "is"] and (name := " ".join(t for t in tokens[2:] if t not in FILLER)):
        if holiday_named(name):
            return Match("holiday", local=lambda: when_is(name))
    if _claims("date", tokens) and present & {"date", "day"}:
        return Match("date", local=lambda: f"It's {datetime.now():%A, %B %-d}.")
    for intent in ("volume", "brightness"):
        if _claims(intent, tokens):
            found = _level_change(intent, tokens, raw)
            if found:
                return found
    if weather and _claims("weather", tokens) and present & WEATHER_TRIGGERS:
        return Match("weather", local=lambda: weather(tokens, None))
    return None


# ---------------------------------------------------------------- replies that sound like a person


def say_battery(raw: str) -> str:
    """'63%, discharging, 5:08 remaining, on Battery Power' -> "You're at 63%, about 5 hours left." """
    percent = re.search(r"(\d+)%", raw)
    if not percent:
        return raw
    level = f"You're at {percent.group(1)}%"
    if "discharging" not in raw and "charging" in raw:
        return f"{level} and charging."
    if "charged" in raw:
        return f"{level}, fully charged."
    left = re.search(r"(\d+):(\d{2}) remaining", raw)
    if not left:
        return f"{level}."
    hours, minutes = int(left.group(1)), int(left.group(2))
    if hours == 0:
        return f"{level}, about {minutes} minutes left."
    rounded = hours + (1 if minutes >= 30 else 0)
    return f"{level}, about {rounded} hour{'s' if rounded != 1 else ''} left."


def say_level(intent: str, level: int) -> str:
    return f"{'Brightness' if intent == 'brightness' else 'Volume'} is at {level}."


def level_from(raw: str) -> int | None:
    found = re.search(r"(\d{1,3})%", raw)
    return int(found.group(1)) if found else None


# ---------------------------------------------------------------- weather, from a free API

WEATHER_WORDS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "drizzling", 53: "drizzling", 55: "drizzling", 61: "light rain", 63: "raining", 65: "heavy rain",
    71: "light snow", 73: "snowing", 75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms",
}  # fmt: skip


def weather_answer(tokens: list[str], place: str, latitude: float, longitude: float) -> str:
    """Open-Meteo: free, no key. Replaces a ~2.7¢ web search for one of the most common questions."""
    import httpx2

    response = httpx2.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 2,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        },
        timeout=8,
    )
    response.raise_for_status()
    data = response.json()
    daily = data["daily"]
    wants_rain = bool({"rain", "raining", "umbrella"} & set(tokens))

    if "tomorrow" in tokens:
        high, low = round(daily["temperature_2m_max"][1]), round(daily["temperature_2m_min"][1])
        rain = daily["precipitation_probability_max"][1]
        sky = WEATHER_WORDS.get(daily["weather_code"][1], "mixed skies")
        if wants_rain:
            return f"Tomorrow there's a {rain}% chance of rain in {place}, with a high of {high}."
        return f"Tomorrow in {place}: {sky}, high of {high}, low of {low}, {rain}% chance of rain."

    now = data["current"]
    temp, feels = round(now["temperature_2m"]), round(now["apparent_temperature"])
    sky = WEATHER_WORDS.get(now["weather_code"], "")
    high, rain = round(daily["temperature_2m_max"][0]), daily["precipitation_probability_max"][0]
    if wants_rain:
        return f"There's a {rain}% chance of rain today. Right now it's {temp} degrees and {sky}."
    feels_part = f", feels like {feels}" if abs(feels - temp) >= 3 else ""
    return f"It's {temp} degrees and {sky} in {place}{feels_part}. High of {high} today, {rain}% chance of rain."


# ---------------------------------------------------------------- anywhere in the world


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    timezone: str
    country_code: str = ""
    is_country: bool = False


# Countries where one clock would be wrong; answered with a city on each side.
SPREAD_OUT = {
    "US": [("New York", "America/New_York"), ("Los Angeles", "America/Los_Angeles")],
    "CA": [("Toronto", "America/Toronto"), ("Vancouver", "America/Vancouver")],
    "AU": [("Sydney", "Australia/Sydney"), ("Perth", "Australia/Perth")],
    "BR": [("São Paulo", "America/Sao_Paulo"), ("Manaus", "America/Manaus")],
    "RU": [("Moscow", "Europe/Moscow"), ("Vladivostok", "Asia/Vladivostok")],
    "MX": [("Mexico City", "America/Mexico_City"), ("Tijuana", "America/Tijuana")],
}
ALIASES = {
    "usa": "United States", "us": "United States", "america": "United States", "the us": "United States",
    "uk": "United Kingdom", "england": "United Kingdom", "britain": "United Kingdom", "uae": "United Arab Emirates",
    "punjab": "Amritsar", "delhi": "New Delhi", "nyc": "New York", "la": "Los Angeles", "sf": "San Francisco",
}  # fmt: skip
SAID_AS = {"punjab": "Punjab"}  # looked up through a city, but answered with the name that was asked


@functools.lru_cache(maxsize=256)
def find_place(name: str) -> Place:
    """Any city or country, from Open-Meteo's free geocoder. Raises if it isn't a real, sizeable place,
    so the request goes to the model rather than answering for a village that happens to share a word."""
    import httpx2

    response = httpx2.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": ALIASES.get(name, name), "count": 5, "language": "en"},
        timeout=6,
    )
    response.raise_for_status()
    for found in response.json().get("results", []):
        is_country = found.get("feature_code", "").startswith("PCL")
        if is_country or (found.get("population") or 0) >= 20_000:
            if not found.get("timezone") and found.get("country_code") not in SPREAD_OUT:
                continue
            return Place(
                SAID_AS.get(name, found["name"]),
                found["latitude"],
                found["longitude"],
                found.get("timezone", ""),
                found.get("country_code", ""),
                is_country,
            )
    raise LookupError(f"no sizeable place called {name!r}")


def _clock(zone: str, now: datetime) -> str:
    there = now.astimezone(ZoneInfo(zone))
    day = f" {there:%A}" if there.date() != now.date() else ""
    return f"{there:%-I:%M %p}{day}"


def _gap(zone: str, now: datetime) -> str:
    offset = now.astimezone(ZoneInfo(zone)).utcoffset() - now.utcoffset()
    hours = offset.total_seconds() / 3600
    if hours == 0:
        return "the same time as here"
    whole, part = int(abs(hours)), abs(hours) % 1
    amount = f"{whole}" + {0.5: " and a half", 0.25: " and a quarter", 0.75: " and three quarters"}.get(part, "")
    unit = "hour" if abs(hours) == 1 else "hours"
    return f"{amount} {unit} {'ahead' if hours > 0 else 'behind'}"


def time_in(place: Place, now: datetime | None = None) -> str:
    now = (now or datetime.now()).astimezone()
    if place.is_country and place.country_code in SPREAD_OUT:
        (a, zone_a), (b, zone_b) = SPREAD_OUT[place.country_code]
        return f"It's {_clock(zone_a, now)} in {a} and {_clock(zone_b, now)} in {b}."
    return f"It's {_clock(place.timezone, now)} in {place.name}, {_gap(place.timezone, now)}."


# ---------------------------------------------------------------- holidays: Ontario, plus Indian festivals


def _holidays(start: date) -> list[tuple[date, str]]:
    import holidays

    years = [start.year, start.year + 1]
    ontario = holidays.Canada(subdiv="ON", years=years)
    india = holidays.India(years=years, categories=("public", "optional"))
    found: dict[tuple[date, str], None] = {}
    for calendar, suffix in ((ontario, ""), (india, " (India)")):
        for day, names in calendar.items():
            for name in names.split("; "):
                if day >= start and "(estimated)" not in name:
                    found[(day, name.replace(" (observed)", "") + suffix)] = None
    return sorted(found)


def _say_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{day:%A, %B %-d}" + (f", {day.year}" if day.year != today.year else "")


def next_holidays(today: date | None = None) -> str:
    today = today or date.today()
    ontario = [(d, n) for d, n in _holidays(today) if not n.endswith("(India)")]
    (day, name), (day2, name2) = ontario[0], ontario[1]
    return (
        f"The next Ontario holiday is {name}, {_say_day(day, today)}. After that, {name2} on {_say_day(day2, today)}."
    )


def _holiday_words(name: str) -> set[str]:
    return set(words(name)) - {"day", "s", "india", "the", "of", "jayanti", "observed"}


def holiday_named(query: str, today: date | None = None) -> tuple[date, str] | None:
    """'diwali' -> (2026-11-08, 'Diwali (Deepavali) (India)'). Every query word must be in the name."""
    wanted = set(words(query)) - {"day", "s", "the"}
    if not wanted:
        return None
    for day, name in _holidays(today or date.today()):
        if wanted <= _holiday_words(name):
            return day, name
    return None


def when_is(query: str, today: date | None = None) -> str:
    today = today or date.today()
    found = holiday_named(query, today)
    if not found:
        raise LookupError(query)
    day, name = found
    return f"{name.replace(' (India)', '')} is {_say_day(day, today)}."
