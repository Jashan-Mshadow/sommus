# 7. Outside content can't trigger outward actions on its own

**Status:** accepted, 2026-09-28

## Context
Sommus reads mail, web pages, PDFs, the clipboard, the screen and other models' answers. Any of them can
carry text planted for an AI ("assistant: text this number…"). The PIN stops strangers, but for ten
minutes after an unlock the model could act on such text: send a message or an email, run a shell
command, or hand a task to Claude Code, which runs with every permission. The prompt already says tool
output is data, not instructions; a prompt is not a guarantee.

## Decision
Enforced in the brain, not the prompt. Once a turn has read from an untrusted tool (`UNTRUSTED_TOOLS` in
`brain/loop.py`), the outward tools (`send_email`, `reply_to_email`, `send_message`, `compose_email`,
`run_shell`, `ask_claude`) only run if Jashan's own request asked for that kind of action: "text her that
I got it" allows a message, "read my latest email" doesn't. The refusal tells the model to report what
the content asked for instead of doing it.

Not a confirmation prompt: Jashan chose full permission with no questions (2026-09-14), and nothing asks
him anything when the request itself was clear.

## Consequences
- "Read this page and email Didi a summary" still works in one go; "read my email" can never end in a
  send.
- It is a word check on the request, so it can be wrong both ways. A refused action is one clear sentence
  to re-ask ("send it"), which is cheap next to an unwanted message or command.
- The marker resets each request, so a follow-up turn that asks directly is never blocked.
