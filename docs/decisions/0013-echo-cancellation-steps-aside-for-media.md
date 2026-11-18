# 13. Echo cancellation steps aside while another app plays sound

2026-11-18. Code: `src/sommus/interfaces/media.py`, `src/sommus/interfaces/duplex.py`. Tests: `tests/test_media.py`.

## The problem

Jashan's Mac audio dipped and came back at random while he watched Netflix or YouTube. Cause: barge-in
(`[voice] barge_in = true`) keeps the mic in macOS voice-processing mode (echo cancellation) on one
`AVAudioEngine`. Voice processing takes over the audio route; on his Bluetooth speaker (Logi Z207) that changes
the output while anything plays. `data/live.log` also showed the watchdog printing "No microphone audio for 11 s:
rebuilding the audio engine" about every 11 s, and each rebuild switched voice processing off and on again.

He wanted barge-in kept (it works well otherwise) and just not applied while a video plays.

## What was decided

1. **Detect other apps' sound** (`media.py`). coreaudiod holds a power assertion per process that outputs sound;
   `pmset -g assertions` shows `Created for PID: <pid>` then `Resources: audio-out <device>`. It needs no
   permissions and works for any app (Chrome, Safari, Spotify, a video file). Measured with `afplay`.
2. **Count output-only assertions.** A process holding `audio-in audio-out` together is not a video:
   `corespeechd` ("Hey Siri") does all day, Sommus's own engine does, and a call does. Counting them kept
   echo cancellation off permanently, Sommus heard its own voice and interrupted itself (first version of this
   change, fixed the same day). Sommus's own processes (`sommus` in the command line, or our pid) are skipped.
3. **Debounce** (`MediaState`): sound must last 2 polls (~4 s) before it counts, and silence 5 polls (~10 s)
   before it is over, so a chime or a paused video doesn't flip the mode.
4. **Two modes in `DuplexAudio`.** While media plays: voice processing off, plain mic through the same engine,
   gain 1.0 (the 2.0 gain compensates for echo cancellation halving the mic). Afterwards: back to voice processing.
   `pause_for_media=True` is the default; no config key.
5. **Recovery.** Measured: switching to plain mode right after voice processing can leave the engine stopped
   (sample rate changes), and another app starting to play stops it too. `_switch_mode()` rebuilds up to 3 times
   and checks mic frames flow. The media thread checks `engine.isRunning()` every 2 s and rebuilds.
6. **Give up cleanly.** If two rounds of rebuilding fail, the process exits with 75 and `sommus-live.command`
   starts a fresh one, the same exit the watchdog uses. The first version rebuilt forever: its rebuilds kept
   refreshing `_rebuilt_at`, so the 8 s watchdog never ran its exit, and Sommus sat deaf for hours.

## Cost and limits

- While media plays there is no echo cancellation. Sommus's own voice reaches the mic, so a spoken reply can be
  heard as speech. `barge_in_needs_name = true` limits the damage (it stops only for its own name), but its voice
  saying "Sommus" could still cut it off. If that shows up, make it wait or stay quiet while media plays.
- Unmeasured: real Netflix/YouTube. Verified with `afplay` over the real engine, silent buffers, and unit tests.
- Unknown: whether the original ~11 s rebuild loop was only a media symptom. The new log lines make it visible:
  "Something is playing sound: echo cancellation off…" and "Echo cancellation back on…".
- A stuck engine after a Bluetooth speaker disconnects ("player did not see an IO cycle") predates this change.

## If it breaks

Set `pause_for_media=False` in the `DuplexAudio(...)` calls in `interfaces/cli.py` (lines ~272 and ~632) to get
the old behaviour back. `tests/test_diary.py::test_the_brain_keeps_one_system_prompt_and_puts_recall_on_the_request`
was already failing before this change.
