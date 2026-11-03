"""Episodic memory: what happened each day, written overnight, so Sommus remembers Jashan across days.

`memory.py` keeps facts he asked to keep. This keeps what a friend would remember without being told: the
midterm on Thursday, the interview he was nervous about, that he was up until 3 finishing a lab. Once a night
the server's scheduler hands the day's turns from the SQLite log to the cheap model, which writes a few
sentences, things worth bringing up later ("follow-ups"), and any lasting facts (those go to memory.py).

Recall costs nothing extra to look up: the last few days and the open follow-ups sit at the end of the cached
system prompt (it changes once a night, when the cache has long expired anyway), and an older day comes back
only when a request shares its uncommon words, as a short note on that request — after the cached prefix, so
it never invalidates it. See docs/decisions/0011.

The diary is a Markdown file in the private vault, next to the memory file: synced by git, editable by hand.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sommus.brain import memory
from sommus.brain.store import Store, Usage

HEADER = (
    "# Sommus Diary\n\n"
    "What Sommus noticed each day, written overnight from the conversation log. Edit or delete freely; "
    "`forget` removes lines too.\n"
)
DAY = re.compile(r"^## (\d{4}-\d{2}-\d{2})\b")
FOLLOW = re.compile(r"^- Follow up(?: (\d{4}-\d{2}-\d{2}))?: (.+?)\s*$")

RECENT_DAYS = 3  # days summarised in the system prompt
RECENT_WITHIN = 7  # ...if they're no older than this
FOLLOW_UPS_SHOWN = 4
UNDATED_FOLLOW_UP_DAYS = 5  # a follow-up with no date is offered for this long after the day it came from
DATED_FOLLOW_UP_DAYS = 3  # ...and a dated one for this long after its date
RECALL_ENTRIES = 2
SUMMARY_CHARS = 500  # per day, as stored
RECALL_CHARS = 300  # per day, when recalled into a request
TRANSCRIPT_CHARS = 14_000  # ~3.5k tokens of the day's turns sent to the summariser
TURN_CHARS = 400
MAX_FACTS_PER_NIGHT = 3

STOP = set(
    """a about after again all also am an and any are around as at be been before being but by can could day did do
    does doing done for from get got had has have he her here him his how i if in into is it its just know let like
    make me more my need no not now of off on one or our out over please really right said say see she should so some
    still tell than thanks that the their them then there these they thing think this those to today tomorrow too
    up us want was we well were what when where which while who why will with would yeah yes yesterday you your
    sommus jashan ok okay hey hi going go""".split()
)


@dataclass
class FollowUp:
    text: str
    on: date | None = None  # the earliest day it's worth bringing up; None = any time soon


@dataclass
class Entry:
    day: date
    summary: str = ""
    follow_ups: list[FollowUp] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"## {self.day.isoformat()} ({self.day:%A})"]
        if self.summary:
            lines.append(self.summary)
        for f in self.follow_ups:
            lines.append(f"- Follow up{' ' + f.on.isoformat() if f.on else ''}: {f.text}")
        return "\n".join(lines)


# ---------------------------------------------------------------- the file


def entries(path: Path | None) -> list[Entry]:
    if not path or not path.exists():
        return []
    found: list[Entry] = []
    current: Entry | None = None
    prose: list[str] = []

    def close() -> None:
        if current is not None:
            current.summary = " ".join(prose).strip()
            found.append(current)

    for line in path.read_text(errors="replace").splitlines():
        if m := DAY.match(line):
            close()
            current, prose = Entry(date.fromisoformat(m.group(1))), []
        elif current is None:
            continue
        elif m := FOLLOW.match(line.strip()):
            current.follow_ups.append(FollowUp(m.group(2), date.fromisoformat(m.group(1)) if m.group(1) else None))
        elif line.strip():
            prose.append(line.strip())
    close()
    return sorted(found, key=lambda e: e.day)


def save(path: Path, all_entries: list[Entry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(e.render() for e in sorted(all_entries, key=lambda e: e.day))
    path.write_text(HEADER + ("\n" + body + "\n" if body else ""))


def put(path: Path, entry: Entry) -> None:
    """Add a day, or replace it when it was already written (a rerun of the same night)."""
    save(path, [e for e in entries(path) if e.day != entry.day] + [entry])


def open_follow_ups(all_entries: list[Entry], today: date) -> list[tuple[Entry, FollowUp]]:
    """Follow-ups worth bringing up today: dated ones from their date for a few days, undated ones for a
    few days after the day they came from. Newest first."""
    out = []
    for entry in reversed(all_entries):
        for f in entry.follow_ups:
            if f.on:
                fresh = f.on <= today <= f.on + timedelta(days=DATED_FOLLOW_UP_DAYS)
            else:
                fresh = entry.day <= today <= entry.day + timedelta(days=UNDATED_FOLLOW_UP_DAYS)
            if fresh:
                out.append((entry, f))
    return out


def _say_day(day: date, today: date) -> str:
    ago = (today - day).days
    if ago == 0:
        return "Today"
    if ago == 1:
        return "Yesterday"
    if ago < 7:
        return f"{day:%A}"
    return f"{day:%B %-d}"


def prompt_block(path: Path | None, user: str, today: date | None = None) -> str:
    """The recent days and open follow-ups, for the end of the system prompt. Changes once a day at most."""
    today = today or date.today()
    known = entries(path)
    recent = [e for e in known if e.day < today and (today - e.day).days <= RECENT_WITHIN and e.summary]
    recent = recent[-RECENT_DAYS:]
    follow = open_follow_ups(known, today)[:FOLLOW_UPS_SHOWN]
    if not recent and not follow:
        return ""
    lines = [f"\n\n--- Your diary: what happened with {user} lately ---"]
    lines += [f"{_say_day(e.day, today)} ({e.day:%a %b %-d}): {e.summary}" for e in recent]
    if follow:
        lines.append("Worth bringing up when it fits:")
        lines += [f"- {f.text}" for _, f in follow]
    return "\n".join(lines)


# ---------------------------------------------------------------- recall


def _words(text: str) -> set[str]:
    out = set()
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        if len(word) < 3 or word in STOP or word.isdigit():
            continue
        out.add(word[:-1] if len(word) > 4 and word.endswith("s") else word)
    return out


def matches(path: Path | None, text: str, today: date | None = None, limit: int = RECALL_ENTRIES) -> list[Entry]:
    """Older days (not already in the prompt) that share uncommon words with this request.

    A day counts when it shares two content words, or one long word (6+ letters) that few days use — a name, a
    course, "interview". Plain word overlap, no embeddings: nothing to load into the server's memory, and good
    enough for "how did the interview go?" finding the day he mentioned the interview."""
    today = today or date.today()
    known = entries(path)
    shown = {
        e.day for e in [e for e in known if e.day < today and (today - e.day).days <= RECENT_WITHIN][-RECENT_DAYS:]
    }
    older = [e for e in known if e.day not in shown and e.day < today]
    wanted = _words(text)
    if not older or not wanted:
        return []
    bags = [(e, _words(e.summary + " " + " ".join(f.text for f in e.follow_ups))) for e in older]
    df = {w: sum(w in bag for _, bag in bags) for w in wanted}
    scored = []
    for entry, bag in bags:
        common = wanted & bag
        if not common:
            continue
        rare = any(len(w) >= 6 and df[w] <= 2 for w in common)
        if len(common) < 2 and not rare:
            continue
        score = sum(math.log(1 + len(bags) / df[w]) for w in common)
        scored.append((score, entry.day, entry))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [entry for _, _, entry in scored[:limit]]


def recall_note(path: Path | None, text: str, today: date | None = None) -> str:
    """The note added to a request when an older day seems related; '' when none does."""
    found = matches(path, text, today)
    if not found:
        return ""
    days = "; ".join(f"{e.day:%a %b %-d}: {e.summary[:RECALL_CHARS]}" for e in found)
    return f"\n[From your diary, in case it's related: {days}]"


def search(path: Path | None, query: str, today: date | None = None) -> str:
    """For the recall_diary tool: a date ('2026-10-12', 'yesterday', 'monday') or words."""
    today = today or date.today()
    known = entries(path)
    if not known:
        return "The diary is empty so far: it's written overnight from the day's conversations."
    q = query.strip().casefold()
    day = None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", q):
        day = date.fromisoformat(q)
    elif q == "yesterday":
        day = today - timedelta(days=1)
    elif q == "today":
        day = today
    else:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        name = q.removeprefix("last ")
        if name in names:
            back = (today.weekday() - names.index(name)) % 7 or 7
            day = today - timedelta(days=back)
    if day is not None:
        found = [e for e in known if e.day == day]
        return found[0].render() if found else f"Nothing in the diary for {day:%A %B %-d}."
    if not q:
        return "\n\n".join(e.render() for e in known[-RECENT_DAYS:])
    wanted = _words(query)
    hits = [e for e in known if wanted & _words(e.render())]
    if not hits:
        return f"Nothing in the diary mentions '{query}'."
    return "\n\n".join(e.render() for e in hits[-5:])


# ---------------------------------------------------------------- forgetting


def forget(path: Path | None, words: str) -> str:
    """Remove the diary sentences and follow-ups containing all of these words; a date removes that whole day."""
    known = entries(path)
    q = words.strip()
    if path and re.fullmatch(r"\d{4}-\d{2}-\d{2}", q):
        day = date.fromisoformat(q)
        if not any(e.day == day for e in known):
            raise memory.MemoryError(f"The diary has nothing for {q}.")
        save(path, [e for e in known if e.day != day])
        return f"Forgot the diary entry for {day:%A %B %-d}."
    wanted = [w for w in re.findall(r"\w+", q.casefold()) if w]
    if not wanted:
        raise memory.MemoryError("Say what to forget.")

    def hit(text: str) -> bool:
        return all(w in text.casefold() for w in wanted)

    gone: list[str] = []
    for e in known:
        sentences = _sentences(e.summary)
        gone += [s for s in sentences if hit(s)]
        gone += [f.text for f in e.follow_ups if hit(f.text)]
    if not gone:
        raise memory.MemoryError(f"Nothing remembered mentions '{words}'.")
    if len(gone) > 3:
        raise memory.MemoryError(f"'{words}' matches {len(gone)} diary lines; be more specific.")
    for e in known:
        e.summary = " ".join(s for s in _sentences(e.summary) if not hit(s))
        e.follow_ups = [f for f in e.follow_ups if not hit(f.text)]
    save(path, [e for e in known if e.summary or e.follow_ups])
    return "Forgot from the diary: " + "; ".join(g.rstrip(".") for g in gone) + "."


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _clean(text: str, limit: int) -> str:
    """One line, no secrets, within `limit`."""
    kept = [s for s in _sentences(" ".join(str(text).split())) if not memory.SECRET.search(s)]
    out = " ".join(kept)
    return out if len(out) <= limit else out[: limit - 1].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- the nightly job

SUMMARISER = """You keep the diary of {name}, {user}'s personal assistant. Below is everything {user} said to \
{name} on {day:%A %B %-d, %Y}, and what {name} answered. Reply with JSON only, with these keys:

"summary": one to four short sentences, past tense, about what {user} did, planned, worried about, enjoyed or \
cared about that day: what a good friend would still remember tomorrow. Name courses, people and places. Skip \
routine device commands (volume, music, brightness, apps, weather lookups) unless they say something about him. \
"" when the day had nothing worth keeping.
"follow_ups": up to 3 things worth bringing up on a later day, like an exam, an interview, a plan, something he \
was waiting on or not feeling well. Each is {{"text": "Ask how the physics midterm went", "on": "YYYY-MM-DD"}}, \
where "on" is the first day it makes sense to bring up (the day after the event), or null for any time soon. \
Not things a reminder already covers.
"facts": up to 3 lasting facts about {user}: routines, preferences, people in his life, how he wants things \
done. Only ones that will still be true in a month and aren't in the list of known facts. One short sentence each.
"done": open follow-ups from the list below that this day's conversation settled (copy their text exactly).

Never include passwords, PINs, codes, keys or other secrets.

Known facts:
{facts}

Open follow-ups:
{follow}"""


def transcript(rows: list[tuple[str, str, str, str]], limit: int = TRANSCRIPT_CHARS) -> str:
    """'21:04 Jashan: … / Sommus: …' lines from (started_at, user_text, reply, model) rows. Gemini Live's
    direct tool calls are logged with the tool's raw output as the reply: only his words are kept for those."""
    lines, seen = [], set()
    for started, said, reply, model in rows:
        if said.startswith("[PIN") or said.startswith("[diary]"):
            continue
        said = re.sub(r"^\[gemini-live\]\s*", "", said)
        reply = "" if model == "gemini-live" else (reply or "")
        key = (said, reply)
        if key in seen:
            continue
        seen.add(key)
        at = started[11:16]
        line = f"{at} Him: {said[:TURN_CHARS]}"
        if reply.strip():
            line += f"\n      You: {' '.join(reply.split())[:TURN_CHARS]}"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > limit:  # keep the start and the end of a very long day
        half = limit // 2
        text = text[:half] + "\n[…]\n" + text[-half:]
    return text


def _json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in the summary")
    found = json.loads(text[start : end + 1])
    if not isinstance(found, dict):
        raise ValueError("the summary isn't a JSON object")
    return found


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


@dataclass
class Written:
    entry: Entry | None
    facts: list[str]
    closed: list[str]
    cost_usd: float


async def write_day(
    store: Store,
    client: Any,
    model: str,
    diary_path: Path,
    memory_path: Path | None,
    day: date,
    since: str,
    until: str,
    name: str = "Sommus",
    user: str = "Jashan",
) -> Written:
    """Distil the turns logged in [since, until) into the diary entry for `day`. No turns, no model call."""
    rows = store.turns_between(since, until)
    text = transcript(rows)
    if not text.strip():
        return Written(None, [], [], 0.0)
    known_facts = memory.facts(memory_path) if memory_path else []
    known = entries(diary_path)
    follow = open_follow_ups(known, day)
    system = SUMMARISER.format(
        name=name,
        user=user,
        day=day,
        facts="\n".join(f"- {f}" for f in known_facts) or "(none)",
        follow="\n".join(f"- {f.text}" for _, f in follow) or "(none)",
    )
    turn_id = store.start_turn(f"[diary] {day.isoformat()}")
    usage = Usage()
    try:
        response = await client.messages.create(
            model=model, max_tokens=800, system=system, messages=[{"role": "user", "content": text}]
        )
        usage.add(response.usage)
        said = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        found = _json(said)
    except Exception:
        store.finish_turn(turn_id, "", "error", model, usage)
        raise
    store.finish_turn(turn_id, "", "ok", model, usage)

    summary = _clean(found.get("summary") or "", SUMMARY_CHARS)
    follow_ups = []
    for item in (found.get("follow_ups") or [])[:3]:
        item = item if isinstance(item, dict) else {"text": item}
        if text_ := _clean(item.get("text") or "", 200).rstrip("."):
            follow_ups.append(FollowUp(text_, _date(item.get("on"))))
    entry = Entry(day, summary, follow_ups) if summary or follow_ups else None

    done = {" ".join(str(d).split()).casefold() for d in found.get("done") or []}
    closed = []
    if done:
        for e in known:
            keep = [f for f in e.follow_ups if f.text.casefold() not in done]
            closed += [f.text for f in e.follow_ups if f.text.casefold() in done]
            e.follow_ups = keep
        save(diary_path, [e for e in known if e.summary or e.follow_ups])
    if entry:
        put(diary_path, entry)

    added = []
    if memory_path:
        for fact in (found.get("facts") or [])[:MAX_FACTS_PER_NIGHT]:
            try:
                if memory.remember(memory_path, str(fact), day).startswith("Remembered"):
                    added.append(" ".join(str(fact).split()).rstrip("."))
            except memory.MemoryError:
                continue  # a secret, too long, or memory full: skip it, keep the rest
    return Written(entry, added, closed, usage.cost_usd(model))


def day_for(now: datetime, at: str) -> date:
    """Which day a run at `at` writes up: a run in the small hours writes up the day before."""
    return (now - timedelta(days=1)).date() if int(at.split(":")[0]) < 12 else now.date()
