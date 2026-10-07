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
7. **Get Dictionary Value** — key `reply` in **Contents of URL**
8. **Speak Text** — the `reply` value

Then **Settings → Action Button → Shortcut → Sommus**.

## Try it

| Say | What happens |
|---|---|
| "What's my next class?" | Spoken answer from the schedule, $0 |
| "Turn on my flashlight" | Flashlight on, $0 |
| "Remind me to take creatine at 9:30" | A Reminders entry, which rings on the phone |
| "What's the battery on my laptop?" | Asks the Mac; if it's asleep, Sommus says so |
| "Text Didi I'm running late" | Looks the number up (needs the Mac awake and the PIN for contacts), then the phone sends it |

## How it answers

`POST /ask {"text": "..."}` returns `{"reply": "...", "action": "", ...}`. `action` is one of `flashlight_on`,
`flashlight_off`, `text` (with `to`, `message`) or `alarm` (with `time`, `label`); empty means just speak the reply.
The phone has its own spoken conversation thread with the brain; Telegram has the written one.
