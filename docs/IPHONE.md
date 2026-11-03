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
     (leave *Show When Run* on at first, so you see the text before it goes)
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

### If you built it before November 2026: add the alarm

The first version of these steps had no `alarm` branch, so Sommus said the alarm was set and the phone threw
the action away (the 1:05 PM alarm on Oct 8 never rang). Open the Shortcut, add step 7 above after the `text`
branch's **End If**, and test it: press the Action Button, say "set an alarm for" a time two minutes from now,
and check Clock → Alarms. The server's log shows what the phone was handed:
`ssh sommus-brain 'journalctl -u sommus -n 20'` → `iPhone: 'set an alarm for …' → alarm`.

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

An iPhone alarm is a time of day: it rings the next time the clock shows it. So `phone_alarm` only takes alarms
within the next 24 hours; for a later day Sommus sets a reminder instead (a Telegram ping at that time).

The phone has its own spoken conversation thread with the brain; Telegram has the written one.
