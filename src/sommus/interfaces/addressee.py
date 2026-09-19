"""Was that said to Sommus, or to someone else in the room?

Once awake, Sommus takes everything it hears as a request — so when Jashan turns to a friend
("Have you seen Jarvis from Iron Man? I'm building that"), it answered the friend's conversation.
A small, fast model now makes the call from what Sommus last said and what was just heard.

Measured 2026-09-16 on 16 hand-written cases (follow-ups vs. room chatter): 16/16 correct, median
0.57 s, about $0.0002 a check. It fails open: if the check errors or is slow, the request runs.
Never called with a PIN in the text — the brain takes those before anything leaves the Mac.
"""

from __future__ import annotations

import asyncio
import re

import anthropic

MODEL = "claude-haiku-4-5"
TIMEOUT_SECONDS = 2.5

SYSTEM = """Sommus is a voice assistant listening in a room where {user} may also be talking to other people. \
Given what Sommus last said and the newest thing the microphone heard, decide whether that newest utterance \
was said TO Sommus (a request, question, answer or follow-up for the assistant) or to someone else in the room \
(chatting, reacting, explaining something to a friend, thinking out loud). Follow-ups like "and tomorrow?", \
"yes", "louder" or "thanks" count as said to Sommus when they fit what Sommus just said. Anything phrased as a \
request or question to a second person ("can you...", "what's my...", "tell me...", "open...") is for Sommus, \
even if {user} asked something like it a moment ago. Only answer no when the utterance is plainly part of a \
conversation with another person, or {user} thinking out loud. When unsure, answer yes. Answer with one word: \
yes or no."""

# Plainly a request: answered here, with no model call. Measured from a real session (2026-09-19), the
# check wrongly rejected "Can you tell me what my to-do list is for a week?" — the commonest kind of
# thing said to a voice assistant — and the request was silently dropped.
DIRECT_REQUEST = re.compile(
    r"^\s*(can|could|would|will|please|tell|give|show|read|open|close|play|pause|set|turn|make|send|remind|find|"
    r"search|look|what|whats|when|where|which|who|how|why|is|are|do|does|did|should|add|write|start|stop|check|"
    r"quit|lock|mute|unmute|skip|call|text|email)\b",
    re.IGNORECASE,
)


def clearly_a_request(text: str) -> bool:
    """A question or instruction aimed at a second person, short enough to be a command."""
    if len(text.split()) > 25:
        return False
    return bool(DIRECT_REQUEST.match(text.strip()))


async def said_to_sommus(client: anthropic.AsyncAnthropic, user: str, last_reply: str, heard: str) -> bool:
    if clearly_a_request(heard):
        return True
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=3,
                system=SYSTEM.format(user=user),
                messages=[
                    {
                        "role": "user",
                        "content": f"Sommus just said: {last_reply or '(nothing yet)'}\nNewest utterance: {heard}",
                    }
                ],
            ),
            TIMEOUT_SECONDS,
        )
    except (TimeoutError, anthropic.APIError):
        return True  # a missed request is worse than an occasional stray reply
    answer = next((b.text for b in response.content if b.type == "text"), "yes")
    return not answer.strip().lower().startswith("no")
