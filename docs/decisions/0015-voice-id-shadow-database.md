# 0015: voice ID starts as a shadow database

2026-11-26. Voice ID (WeSpeaker embeddings, `speaker.py`) was parked on 2026-10-06: a voiceprint made from the plain
microphone scored his voice 0.52 to 0.67 through live mode's echo cancellation, and enrolling took too long.

**Decision:** collect his voice from live mode itself, quietly, and keep the PIN as the only thing that unlocks
anything. `voicedb.py` stores, for each sentence said in an open conversation, its embedding (numbers), length,
loudness, a label for how sure we are it is him (PIN said aloud and accepted / inside the PIN window / any other),
and the score against the parked print. Short WAV clips only for the surest labels, capped at 200, so another model
can be tried later. Everything is in `data/` on the Mac. `voiceprint.json` is never written or read by this.

**Why shadow first:** real data comes from the audio path that failed before, no enrolment session is needed, and
the report (`sommus voice-db`) can say whether his voice separates from strangers before anything depends on it.

**Ready means:** 200 sentences on 5 days, 10 PIN-confirmed sentences, and every one of those inside his cluster.
Then `sommus voice-db build` writes `voiceprint.candidate.json`, which nothing reads. Turning it on (renaming it
to `voiceprint.json`) is a separate decision, and the first tiers would be low-risk actions only.

**Not done:** negative samples (other people's voices) are not collected; strangers are only inferred as outliers.
A false accept rate cannot be measured until there are real other-voice recordings (a roommate saying the wake
word on purpose, with permission).
