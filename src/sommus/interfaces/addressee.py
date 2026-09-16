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

import anthropic

MODEL = "claude-haiku-4-5"
TIMEOUT_SECONDS = 2.5

SYSTEM = """Sommus is a voice assistant listening in a room where {user} may also be talking to other people. \
Given what Sommus last said and the newest thing the microphone heard, decide whether that newest utterance \
was said TO Sommus (a request, question, answer or follow-up for the assistant) or to someone else in the room \
(chatting, reacting, explaining something to a friend, thinking out loud). Follow-ups like "and tomorrow?", \
"yes", "louder" or "thanks" count as said to Sommus when they fit what Sommus just said. Answer with one word: \
yes or no."""


async def said_to_sommus(client: anthropic.AsyncAnthropic, user: str, last_reply: str, heard: str) -> bool:
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
