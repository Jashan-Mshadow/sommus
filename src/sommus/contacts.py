"""Contacts on the always-on brain, so "text Didi" and "what's Mom's number" work with the laptop asleep.

The Contacts app only exists on the Mac. The server copies it through the laptop node's `list_contacts`
whenever the Mac is reachable (at start, then every few hours) into data/contacts.txt, and answers
`find_contact` / `list_contacts` itself from that copy. On the server these replace the laptop's own tools of
the same name.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# One line of the laptop's list_contacts: "Didi — phone (647) 832-5827 — email didi@example.com"
LINE = re.compile(r"^(?P<name>[^—]+?)(?:\s+—\s+phone\s+(?P<phones>[^—]+?))?(?:\s+—\s+email\s+(?P<emails>.+?))?\s*$")


@dataclass(frozen=True)
class Contact:
    name: str
    phones: tuple[str, ...]
    emails: tuple[str, ...]

    def __str__(self) -> str:
        parts = [self.name]
        if self.phones:
            parts.append("phone " + ", ".join(self.phones))
        if self.emails:
            parts.append("email " + ", ".join(self.emails))
        return " — ".join(parts)


def parse(listing: str) -> list[Contact]:
    people = []
    for line in listing.splitlines():
        line = line.strip()
        if not line or re.match(r"^\d+ contacts:$", line):
            continue
        if m := LINE.match(line):
            split = lambda s: tuple(x.strip() for x in (s or "").split(",") if x.strip())  # noqa: E731
            people.append(Contact(m["name"].strip(), split(m["phones"]), split(m["emails"])))
    return people


def find(people: list[Contact], query: str) -> list[Contact]:
    """Same rules as the Mac: an exact name, then part of a name, then any word of it ('didi', 'mom')."""
    needle = query.casefold().strip()
    exact = [c for c in people if c.name.casefold() == needle]
    if exact:
        return exact
    parts = [c for c in people if needle in c.name.casefold()]
    if parts:
        return parts
    return [c for c in people if any(word.startswith(needle) for word in c.name.casefold().split())]


class ContactBook:
    def __init__(self, path: Path):
        self.path = path
        self.people: list[Contact] = []
        self.copied: str = ""
        with contextlib.suppress(OSError):
            text = path.read_text()
            self.people = parse(text)
            self.copied = datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %-d, %-I:%M %p")

    def replace(self, listing: str, now: datetime) -> int:
        # Only the laptop's real list ("16 contacts:" first): an error message must never become the copy.
        if not re.match(r"^\d+ contacts:", listing.strip()):
            return 0
        people = parse(listing)
        if not people:
            return 0
        self.people, self.copied = people, now.strftime("%b %-d, %-I:%M %p")
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(str(c) for c in people) + "\n")
        return len(people)

    def node(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("contacts", log_level="WARNING")
        read = ToolAnnotations(read_only_hint=True)

        @server.tool(annotations=read, structured_output=False)
        def find_contact(name: str) -> str:
            """Look up someone's phone number and email in Contacts. Use this before messaging or emailing a person.

            Args:
                name: All or part of the name, e.g. "Kuljot", "didi", "mom".
            """
            if not self.people:
                return "No copy of the contacts yet: it's made from the Mac the next time it's awake."
            found = find(self.people, name)
            if not found:
                return f"Nobody in Contacts matches '{name}'. Saved contacts: " + ", ".join(c.name for c in self.people)
            return "\n".join(str(c) for c in found)

        @server.tool(annotations=read, structured_output=False)
        def list_contacts() -> str:
            """List everyone in Contacts with their numbers and emails."""
            if not self.people:
                return "No copy of the contacts yet: it's made from the Mac the next time it's awake."
            return f"{len(self.people)} contacts (copied from the Mac {self.copied}):\n" + "\n".join(
                str(c) for c in self.people
            )

        return server
