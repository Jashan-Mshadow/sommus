# Sommus on the iPhone's Action Button

Press the Action Button, talk, and Sommus answers out loud: from anywhere, with the laptop closed. The phone
sends what you said to the always-on brain over Tailscale (`/ask`), speaks the reply, and does the phone-only
part itself (flashlight, a text from the phone, an alarm). No token on the phone: only devices on your own
tailnet can reach `/ask`, and the server checks each caller with `tailscale whois`.

Needs: the Tailscale app on the iPhone, signed in, VPN on.

## Build the Shortcut (about 10 minutes)

Shortcuts app → **+** → name it **Sommus**. Add these actions in order (search for each name):

1. **Dictate Text** — Language: English · Stop Listening: **After Pause**
2. **Get Contents of URL**
   - URL: `http://100.101.226.81:8780/ask`
   - Tap the arrow (Show More): Method **POST** · Request Body **JSON**
   - Add new field → **Text** · Key `text` · Value: the **Dictated Text** variable
3. **Get Dictionary Value** — Get **Value** for key `action` in **Contents of URL**
4. **If** — *Dictionary Value* **is** `flashlight_on` → inside: **Set Flashlight** → **On** · then **End If**
5. **If** — *Dictionary Value* **is** `flashlight_off` → inside: **Set Flashlight** → **Off** · **End If**
6. **If** — *Dictionary Value* **is** `text` → inside:
   - **Get Dictionary Value** — key `message` in **Contents of URL**
   - **Get Dictionary Value** — key `to` in **Contents of URL**
   - **Send Message** — Message: the `message` value · Recipients: the `to` value
     (tap the arrow on the action and turn **Show When Run OFF**, or it waits for you to press Send; if it still asks, open the Shortcut's ⓘ settings and allow it to run while the phone is locked)
   - **End If**
7. **If** — *Dictionary Value* (the `action` one from step 3) **is** `alarm` → inside:
   - **Get Dictionary Value** — key `time` in **Contents of URL** (always 24-hour, like `13:05`)
   - **Get Dates from Input** — input: that `time` value (turns `13:05` into a time Clock understands)
   - **Get Dictionary Value** — key `label` in **Contents of URL**
   - **Create Alarm** (Clock) — Time: **Dates** · Label: the `label` value
     (leave *Show When Run* on at first, so you see the alarm it makes)
   - **End If**
8. **Get Dictionary Value** — key `reply` in **Contents of URL**
9. **Speak Text** — the `reply` value

When a step picks a variable, tap it and choose the exact action it came from: with several *Get Dictionary
Value* steps, "Dictionary Value" otherwise means the most recent one, and the `If` checks compare the wrong value.

Then **Settings → Action Button → Shortcut → Sommus**.

### Music and Focus

Two more branches, same pattern as `alarm` (add them after the alarm's **End If**, before *Get Dictionary Value → reply*):

- **If** `action` is `music` → **Get Dictionary Value** keys `control` and `what`. Then: `control` is `pause` → **Play/Pause**;
  `next` → **Skip Forward**; `previous` → **Skip Back**; `play` with a `what` → **Find Playlists** (Name contains `what`)
  → **Play Music** (use **Find Music** for a song or artist); `play` with nothing → **Play/Pause**.
- **If** `action` is `focus_on` (and another for `focus_off`) → **Get Dictionary Value** key `mode`. Then one **If** per
  Focus you use (`Do Not Disturb`, `Sleep`, `Study`...) → **Set Focus** for that Focus, On or Off.

Say "play my Lofi playlist" or "turn on Do Not Disturb" through the Action Button to try them.

### If you built it before November 2026: add the alarm

The first version of these steps had no `alarm` branch, so Sommus said the alarm was set and the phone threw
the action away (the 1:05 PM alarm on Oct 8 never rang). Open the Shortcut, add step 7 above after the `text`
branch's **End If**, and test it: press the Action Button, say "set an alarm for" a time two minutes from now,
and check Clock → Alarms. The server's log shows what the phone was handed:
`ssh sommus-brain 'journalctl -u sommus -n 20'` → `iPhone: 'set an alarm for …' → alarm`.

## Version 2: several actions per request (scenes, timers, apps, directions)

The steps above run one `action` per request, and they keep working unchanged. Version 2 lets one request do
several phone things ("goodnight" = Sleep Focus on the phone while the lamp and the Mac do their part), and adds
timers, opening apps and links, volume, brightness, directions and running one of your own Shortcuts. The server
only queues more than one action when the Shortcut says it can loop, so build all of this before turning it on.

**1. Tell the server you loop.** In step 2 (**Get Contents of URL**), add another field: **Number** · Key `v` ·
Value `2`. (A Number, not Text: a Text field could be read as what you said when dictation comes back empty.)

**2. Replace steps 3–7 with one loop.** Delete the old *Get Dictionary Value → action* step and its `If` branches
(or keep them in a copy of the Shortcut as a backup), then add:

1. **Get Dictionary Value** — Get **Value** for key `actions` in **Contents of URL**
2. **Repeat with Each** — in **Dictionary Value** (the `actions` list). Inside the repeat:
   1. **Get Dictionary Value** — key `action` in **Repeat Item**. Rename this step's output *Kind*
      (tap the variable → Rename) so the `If`s below can't pick the wrong one.
   2. One **If** per row of the table below: *Kind* **is** the name in the first column → inside, the actions in
      the last column, each reading its keys with **Get Dictionary Value** · key · in **Repeat Item** → **End If**.
3. **End Repeat**, then the old steps 8–9 (**Get Dictionary Value** `reply` → **Speak Text**) as before.

| `action` | keys | Shortcut actions inside the If |
|---|---|---|
| `flashlight_on` / `flashlight_off` | — | **Set Flashlight** On / Off |
| `text` | `to`, `message` | **Send Message** (Message `message`, Recipients `to`). Leave **Show When Run ON** here: you tap Send, which is the confirmation |
| `alarm` | `time` (24-hour `13:05`), `label` | **Get Dates from Input** on `time` → **Create Alarm** (Time: Dates, Label `label`) |
| `music` | `control`, `what` | `control` is `pause` → **Play/Pause**; `next` → **Skip Forward**; `previous` → **Skip Back**; `play` with a `what` → **Find Playlists** (Name contains `what`) → **Play Music**; `play` alone → **Play/Pause** |
| `focus_on` / `focus_off` | `mode` | one **If** per Focus you use (`Sleep`, `Do Not Disturb`, `Study`...) → **Set Focus** On / Off |
| `open_app` | `app` | **Open App** — tap the app field, choose **Select Variable**, pick `app` (if your iOS won't take a variable there, add an **If** per app you use) |
| `open_url` | `url` | **Open URLs** — `url` |
| `timer` | `seconds`, `minutes`, `label` | **Start Timer** — `seconds`, unit **seconds** |
| `volume` | `level` (0–100), `fraction` (0–1) | **Set Volume** — `level`. If the phone jumps to full volume, use `fraction` instead |
| `brightness` | `level`, `fraction` | **Set Brightness** — `level` (same note as volume) |
| `navigate` | `to`, `mode`, `url` | **Open URLs** — `url` (Apple Maps opens with directions; press Go to start) |
| `shortcut` | `name`, `input` | **Run Shortcut** — Shortcut: `name` (Select Variable), Input: `input` |

`sleep` never shows up in `actions`: it stays in `action` as before ("you're good to go", silence, hard sleep),
and those answers have no `actions` at all, which the loop simply runs zero times.

**3. Try it.** Press the Action Button and say "set a timer for 2 minutes" (a Clock timer, not a reminder), "open
Spotify", "goodnight" (the lamp goes off, the Mac goes quiet and dark, the phone goes into Sleep Focus), and
"directions to E7". The server log shows the whole list: `iPhone: 'goodnight' → focus_on`.

Without step 1 (no `"v": 2`), the server still hands over one action, as before, and a scene that wanted a second
phone action says so in its reply ("your Shortcut takes one action per request").

## Scenes and which device answers

A scene is one phrase for several devices at once, defined in `config.toml` under `[scenes]` (edit it there; no
restart). The defaults: **goodnight** (lamp off, Mac quiet, Mac screen off, Sleep Focus on the phone), **study
mode** (Mac quiet, volume 20, VS Code open, Do Not Disturb on the phone) and **I'm leaving** (lamp off, Mac locked).
Say the name alone ("goodnight", "run study mode", "start my goodnight scene"); "what scenes do I have" lists them.
Each device reports for itself: "Lamp off, Mac quiet, Mac screen off. Phone: skipped, it only acts while you're
using the Action Button. Goodnight." A Mac that's asleep is "Mac: couldn't reach it", and the rest still happens.
A step that needs the PIN is skipped and named, never half-asked.

Through the Action Button, "play music", "pause", "next song", "set a timer for 10 minutes" and "open Spotify"
happen on the phone. From Telegram or the Mac they happen on the Mac (a timer from Telegram is a Telegram reminder).
The phone can only act while you are using the Action Button: asked from Telegram, Sommus says it didn't happen.

## Try it

| Say | What happens |
|---|---|
| "What's my next class?" | Spoken answer from the schedule, $0 |
| "Turn on my flashlight" | Flashlight on, $0 |
| "Remind me to take creatine at 9:30" | A Reminders entry, which rings on the phone |
| "Set an alarm for 6:45" | An alarm in Clock for the next 6:45 |
| "What's the battery on my laptop?" | Asks the Mac; if it's asleep, Sommus says so |
| "Text Didi I'm running late" | Looks the number up (needs the Mac awake and the PIN for contacts), then the phone sends it |

## How it answers

`POST /ask {"text": "..."}` returns `{"reply": "...", "action": "", ...}`. `action` is one of `flashlight_on`,
`flashlight_off`, `text` (with `to`, `message`), `alarm` (with `time` as `HH:MM` 24-hour in Waterloo time,
`at` as the full ISO date and time it rings, and `label`) or `sleep` (nothing was said, or "you're good to go":
stop listening); empty means just speak the reply. One action per request: each `/ask` starts with none queued.

Version 2 is additive: every answer the brain gives also carries `actions`, a list of `{"action": ..., keys...}` in the order to
run them (vocabulary in the table above, and in `src/sommus/phone_more.py`). `action` and its keys at the top
level are always the list's first entry, so an older Shortcut sees exactly what it did before. Only a request
that sends `"v": 2` can get more than one entry (at most 8).

An iPhone alarm is a time of day: it rings the next time the clock shows it. So `phone_alarm` only takes alarms
within the next 24 hours; for a later day Sommus sets a reminder instead (a Telegram ping at that time).

The phone has its own spoken conversation thread with the brain; Telegram has the written one.
