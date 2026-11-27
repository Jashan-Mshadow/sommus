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
   - **Get Contents of URL** — URL `http://100.101.226.81:8780/phone/done` · Method **POST** · Request Body
     **JSON** · field **Text**, key `action`, value `alarm`. This tells Sommus the alarm was really made. Until
     the phone has confirmed the last alarm it was handed, Sommus says "it may not ring, check Clock" instead of
     "alarm set", because it can't see the phone.
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

### Alarms don't show up in Clock? Fix step 7

The first version of these steps had no `alarm` branch, so Sommus said the alarm was set and the phone threw
the action away (the 1:05 PM alarm on Oct 8 never rang). It happened again on 2026-11-26 (a 7:50 PM test alarm:
the server handed the phone `alarm`, Clock showed nothing), so the Shortcut on the phone still lacks a working
alarm branch. Open the Shortcut and check step 7:

1. The branch exists, after the `text` branch's **End If**.
2. Its **If** compares the `action` value from step 3, not a later *Dictionary Value* (tap the variable and pick
   the step 3 one).
3. The **Get Contents of URL → /phone/done** step is the last one inside the branch.

Test it: press the Action Button, say "set an alarm for" a time two minutes from now, and check Clock → Alarms.
The server's log shows what the phone was handed and whether it confirmed:
`ssh sommus-brain 'journalctl -u sommus -n 20'` → `iPhone: 'set an alarm for …' → alarm`, then
`iPhone confirmed: alarm`. From then on Sommus says "Setting an alarm on your phone", and a wake-up set from the
phone ("wake me up at 7:50") also puts a backup alarm in Clock.

## Wake-ups work without the Shortcut

"Wake me up at 7:50" (by Telegram, the Mac or the phone) is kept on the server, not the phone: at the time Sommus
says it out loud on the Mac (turned up and unmuted for it, even late at night, never during a class) and sends it
to Telegram with an "I'm up" button, every 2 minutes, 3 times, until you answer. Any message to Sommus counts as an
answer. If the Mac is asleep only Telegram rings, so keep Telegram's notification sound on. The phone itself can't
be woken by the server: it only does things inside an Action Button request. Phone alarms can't be set from
Telegram either, and Sommus says so.

## Try it

| Say | What happens |
|---|---|
| "What's my next class?" | Spoken answer from the schedule, $0 |
| "Turn on my flashlight" | Flashlight on, $0 |
| "Remind me to take creatine at 9:30" | A Reminders entry, which rings on the phone |
| "Set an alarm for 6:45" | An alarm in Clock for the next 6:45 (needs step 7; until it has confirmed one, Sommus says it may not ring) |
| "Wake me up at 7:50" | Said on the Mac and sent to Telegram at 7:50, repeated until you answer, $0 |
| "What's the battery on my laptop?" | Asks the Mac; if it's asleep, Sommus says so |
| "Text Didi I'm running late" | Looks the number up (needs the Mac awake and the PIN for contacts), then the phone sends it |

## How it answers

`POST /ask {"text": "..."}` returns `{"reply": "...", "action": "", ...}`. `action` is one of `flashlight_on`,
`flashlight_off`, `text` (with `to`, `message`), `alarm` (with `time` as `HH:MM` 24-hour in Waterloo time,
`at` as the full ISO date and time it rings, and `label`; the Shortcut then POSTs `{"action": "alarm"}` to
`/phone/done`) or `sleep` (nothing was said, or "you're good to go":
stop listening); empty means just speak the reply. One action per request: each `/ask` starts with none queued.

An iPhone alarm is a time of day: it rings the next time the clock shows it. So `phone_alarm` only takes alarms
within the next 24 hours; for a later day Sommus sets a reminder instead (a Telegram ping at that time).

The phone has its own spoken conversation thread with the brain; Telegram has the written one.
