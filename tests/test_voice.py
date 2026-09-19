"""Voice interface: what gets spoken, and when listening stops."""

import asyncio
import threading

import numpy as np
import pytest

from sommus.interfaces import voice


def test_markdown_and_links_are_not_read_aloud():
    text = "**Done** — opened [the notes](https://learn.uwaterloo.ca/x) · see `config.toml`"
    assert voice.clean_for_speech(text) == "Done, opened the notes, see config.toml"


def test_bare_urls_become_a_link():
    assert voice.clean_for_speech("Saved to https://example.com/a/b?c=1.") == "Saved to a link"


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("It's 2:45 PM.", "It's 2 45 PM."),
        ("Class at 3:00 PM.", "Class at 3 PM."),
        ("It's 14°C and cloudy.", "It's 14 degrees and cloudy."),
        ("Low of -2°.", "Low of -2 degrees."),
        ("ECE198 Lab 8:30–10:20 (E7 1427/E2 1792)", "ECE198 Lab 8 30 to 10 20 (E7 1427 or E2 1792)"),
        ("Tesla event 6:30-7:30 PM.", "Tesla event 6 30 to 7 30 PM."),
    ],
)
def test_times_and_temperatures_read_naturally(written, spoken):
    assert voice.clean_for_speech(written) == spoken


class FakeVoice:
    """Records what would be said; `play` blocks until released, like real audio does."""

    def __init__(self, hold=False):
        self.synthesized: list[str] = []
        self.played: list[str] = []
        self.cut_off: list[str] = []
        self.hold = hold
        self.playing = threading.Event()

    def synthesize(self, sentence):
        self.synthesized.append(sentence)
        if sentence == "broken":
            raise RuntimeError("voice fell over")
        return sentence.upper()

    def play(self, clip, stop):
        self.playing.set()
        if self.hold and stop.wait(2):
            self.cut_off.append(clip)
            return
        self.played.append(clip)

    def rest(self, stop):
        self.played.append("<dropped>" if stop.is_set() else "<rest>")


async def test_replies_are_spoken_sentence_by_sentence():
    engine = FakeVoice()
    speaker = voice.Speaker(engine)
    for delta in ["Brightness is ", "at 40%. Volume", " is muted! Anything", " else"]:
        speaker.feed(delta)
    speaker.flush()
    await speaker.finished()
    await speaker.close()

    assert engine.played == ["BRIGHTNESS IS AT 40%.", "VOLUME IS MUTED!", "ANYTHING ELSE", "<rest>"]
    assert speaker.first_word_at is not None


async def test_interrupt_cuts_off_the_sentence_and_drops_the_rest():
    engine = FakeVoice(hold=True)
    speaker = voice.Speaker(engine)
    speaker.feed("First sentence. Second sentence. Third")
    await asyncio.to_thread(engine.playing.wait, 2)
    speaker.interrupt()
    await asyncio.wait_for(speaker.finished(), 2)

    assert engine.cut_off == ["FIRST SENTENCE."] and engine.played == ["<dropped>"]
    assert speaker.buffer == ""  # the unfinished "Third" isn't spoken later either

    engine.hold = False
    speaker.feed("Next reply.")
    speaker.flush()
    await asyncio.wait_for(speaker.finished(), 2)
    await speaker.close()
    assert engine.played == ["<dropped>", "<dropped>", "NEXT REPLY.", "<rest>"]


async def test_a_voice_error_is_reported_and_speech_carries_on():
    engine, errors = FakeVoice(), []
    speaker = voice.Speaker(engine, on_error=errors.append)
    speaker.feed("broken\nStill here.")
    speaker.flush()
    await asyncio.wait_for(speaker.finished(), 2)
    await speaker.close()

    assert [str(e) for e in errors] == ["voice fell over"] and engine.played == ["STILL HERE.", "<rest>"]


def test_voice_config_picks_the_engine():
    kokoro = voice.make_voice({"kokoro_voice": "bm_george", "speed": 1.1})
    assert isinstance(kokoro, voice.KokoroVoice) and (kokoro.voice, kokoro.speed) == ("bm_george", 1.1)
    say = voice.make_voice({"engine": "say", "say_voice": "Samantha (Enhanced)", "say_rate": 200})
    assert isinstance(say, voice.SayVoice) and (say.voice, say.rate) == ("Samantha (Enhanced)", 200)


@pytest.mark.parametrize("name", ["jf_alpha", "zm_yunxi", "heart", "af_heart; rm -rf"])
def test_only_english_kokoro_voices_are_accepted(name):
    with pytest.raises(ValueError, match="English"):
        voice.KokoroVoice(name)


def test_say_plays_the_sentence_and_stops_when_told(monkeypatch):
    commands = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.killed = False

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self):
            return -9

    monkeypatch.setattr(voice.subprocess, "Popen", FakeProcess)
    stop = threading.Event()
    stop.set()
    voice.SayVoice("Samantha", 190).play("Hello there.", stop)
    assert commands == [["say", "-v", "Samantha", "-r", "190", "Hello there."]]


def chunks(pattern):
    """[(probability, seconds), ...] -> the chunk-by-chunk probabilities a detector would see."""
    return [p for p, seconds in pattern for _ in range(round(seconds / voice.CHUNK_SECONDS))]


def run_detector(pattern, **kwargs):
    scores = iter(chunks(pattern))
    detector = voice.SpeechDetector(probability=lambda chunk: next(scores), **kwargs)
    out = []
    for i in range(len(chunks(pattern))):
        found = detector.feed(np.full(voice.CHUNK, i, dtype=np.float32))
        if found is not None:
            out.append(found)
    return out, detector


def test_an_utterance_ends_after_a_pause_and_keeps_its_first_syllable():
    found, _ = run_detector([(0.0, 1.0), (0.9, 1.0), (0.0, 1.0)], silence_seconds=0.9, pre_roll_seconds=0.3)
    assert len(found) == 1
    seconds = len(found[0]) / voice.SAMPLE_RATE
    assert 0.3 + 1.0 + 0.9 - 0.1 <= seconds <= 0.3 + 1.0 + 0.9 + 0.1  # pre-roll + speech + the pause that ended it


def test_a_breath_mid_sentence_does_not_split_it():
    found, _ = run_detector([(0.9, 1.0), (0.1, 0.4), (0.9, 1.0), (0.0, 1.0)], silence_seconds=0.9)
    assert len(found) == 1


def test_a_click_or_cough_is_not_an_utterance():
    found, _ = run_detector([(0.0, 0.5), (0.9, 0.1), (0.0, 1.5)], min_speech_seconds=0.25)
    assert found == []


def test_nonstop_talk_is_cut_at_the_limit():
    found, _ = run_detector([(0.9, 5.0)], max_seconds=2.0)
    assert len(found) == 2


def test_an_utterance_in_progress_is_visible():
    """The conversation mustn't fall asleep on someone who has just started talking."""
    _, detector = run_detector([(0.0, 0.5), (0.9, 0.5)])
    assert detector.buffer


WAKE = voice.wake_pattern(["sommus", "hey sommus", "what's up sommus", "yo sommus"])


@pytest.mark.parametrize(
    ("heard", "asked"),
    [
        ("Hey Sommus, what time is it?", "what time is it?"),
        ("Sommus.", ""),
        ("What's up, Sommus?", ""),
        ("Yo Sommus turn the volume up", "turn the volume up"),
        ("What time is it, Sommus?", "What time is it"),
        ("hey, sommus! mute", "mute"),
        ("What's up so missy?", ""),  # found synthesising the wake phrases and transcribing them
        ("Yo, so miss, what are my classes tomorrow?", "what are my classes tomorrow?"),
        # talk to a friend merged into the same utterance, then the name set off by pauses (from the log)
        ("Oh, oh my god, see? So, wait, so miss, what's my battery right now?", "what's my battery right now?"),
    ],
)
def test_wake_phrases_at_the_start_or_end_wake_it(heard, asked):
    assert voice.heard_wake(heard, WAKE) == asked


@pytest.mark.parametrize("heard", ["I'm working on Sommus tonight.", "What's the weather?", "Sommusy", "hey summer"])
def test_the_name_in_passing_does_not_wake_it(heard):
    assert voice.heard_wake(heard, WAKE) is None


@pytest.mark.parametrize("said", ["That's all.", "Thanks", "never mind", "Go to sleep.", "stop listening", "I'm good"])
def test_ending_the_conversation(said):
    assert voice.DISMISS.match(said)


@pytest.mark.parametrize("said", ["thanks, now mute", "that's all the classes?", "done with the email, send it"])
def test_requests_that_merely_start_like_a_goodbye_still_run(said):
    assert not voice.DISMISS.match(said)


async def test_the_speaker_says_when_it_is_talking_so_the_mic_can_ignore_it():
    engine = FakeVoice(hold=True)
    speaker = voice.Speaker(engine)
    assert not speaker.speaking
    speaker.feed("Hello there. ")
    assert speaker.speaking
    await asyncio.to_thread(engine.playing.wait, 2)
    speaker.interrupt()
    await asyncio.wait_for(speaker.finished(), 2)
    assert not speaker.speaking and speaker.quiet_since > 0
    await speaker.close()


@pytest.mark.parametrize("heard", ["Thank you.", "you", "", " Thanks for watching!"])
def test_whisper_hallucinations_on_silence_are_dropped(monkeypatch, heard):
    import sys
    import types

    fake = types.SimpleNamespace(transcribe=lambda *a, **k: {"text": heard})
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    assert voice.Transcriber("model").transcribe(np.zeros(100, dtype=np.float32)) == ""


@pytest.mark.parametrize("engine_voice", ["bf_isabella", "af_heart"])
def test_kokoro_is_told_how_to_say_sommus(engine_voice):
    class FakeModel:
        def generate(self, text, **kwargs):
            self.text = text
            return []

    engine = voice.KokoroVoice(engine_voice)
    engine.model = FakeModel()
    engine.synthesize("Sommus is listening, and sommus's voice works.")
    phonemes = "sˈQmɪs" if engine_voice.startswith("b") else "sˈOmɪs"
    assert engine.model.text == f"[Sommus](/{phonemes}/) is listening, and [Sommus](/{phonemes}/)'s voice works."


@pytest.mark.parametrize(
    "heard", ["Hey Somis, mute.", "Hey Sommis, mute.", "Hey Samus, mute.", "Hey Sowmiss, mute.", "Hey, So Miss, mute."]
)
def test_whisper_spellings_of_the_name_are_fixed(monkeypatch, heard):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=lambda *a, **k: {"text": heard}))
    assert voice.Transcriber("model").transcribe(np.zeros(100, dtype=np.float32)) in (
        "Hey Sommus, mute.",
        "Sommus, mute.",
    )


def test_microphone_audio_at_another_rate_becomes_16k():
    one_second = np.sin(np.linspace(0, 440 * 2 * np.pi, 44_100)).astype(np.float32)
    converted = voice.to_16k(one_second, 44_100)
    assert converted.dtype == np.float32 and abs(len(converted) - 16_000) <= 1


COURSES = {
    "ECE 105": "physics",
    "COMMST 192": "communications",
    "ECE 198": "project studio",
    "MATH 115": "linear algebra",
}


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("ECE105 LEC 001, 8:30–9:20 in E7 5353", "physics lecture, 8 30 to 9 20 in E7 5353"),
        ("COMMST192 Lecture 002 at 1:00 PM", "communications Lecture at 1 PM"),
        ("ECE 198 Lab 001 then MATH115 TUT 103", "project studio lab then linear algebra tutorial"),
        ("Class at 10:05.", "Class at 10 oh 5."),
    ],
)
def test_schedules_are_read_the_way_people_say_them(written, spoken):
    assert voice.clean_for_speech(written, COURSES) == spoken


def test_the_listener_lets_go_of_the_mic_while_sommus_talks():
    """Laptop speakers are inches from the mic: without deafness, Sommus answers itself. And the
    mic must be closed before playback restarts PortAudio, or it silently stops hearing."""
    pattern = [(0.0, 0.5), (0.9, 1.0), (0.0, 1.0), (0.9, 1.0), (0.0, 1.0)]  # two utterances
    scores = chunks(pattern)
    talking = {"now": False}

    class FakeStream:
        def __init__(self):
            self.i = 0

        def read(self, n):
            talking["now"] = round(2.5 / voice.CHUNK_SECONDS) <= self.i  # Sommus starts talking here
            self.i += 1
            return np.full((n, 1), scores[min(self.i, len(scores)) - 1], dtype=np.float32), False

    heard = []
    detector = voice.SpeechDetector(probability=lambda chunk: float(chunk[0]))
    listener = voice.Listener(detector, deliver=heard.append, deaf=lambda: talking["now"])
    listener._listen(FakeStream(), voice.SAMPLE_RATE)  # returns as soon as Sommus talks
    assert len(heard) == 1 and not detector.buffer


def test_playback_waits_for_the_mic_to_close_before_restarting_audio(monkeypatch):
    import sys
    import threading
    import types

    events = []
    fake_sd = types.SimpleNamespace(_terminate=lambda: events.append("restart"), _initialize=lambda: None)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    mic_open, release = threading.Event(), threading.Event()

    def listener():
        with voice.AUDIO_DEVICES:
            mic_open.set()
            release.wait(2)
            events.append("mic closed")

    thread = threading.Thread(target=listener)
    thread.start()
    mic_open.wait(2)
    player = threading.Thread(target=voice._refresh_audio_devices)
    player.start()
    release.set()
    player.join(2)
    thread.join(2)
    assert events == ["mic closed", "restart"]


@pytest.mark.parametrize(
    "heard", ["Remind me to.", "Text mom and", "Set a timer for", "What's the weather in, um", "Open the..."]
)
def test_a_sentence_that_stops_mid_thought_waits_for_the_rest(heard):
    assert voice.sounds_unfinished(heard)


@pytest.mark.parametrize(
    "heard",
    ["Turn it on.", "What's the weather like?", "What are you up to?", "Mute.", "2684", "Open Spotify.", ""],
)
def test_complete_requests_run_straight_away(heard):
    assert not voice.sounds_unfinished(heard)


def test_quiet_audio_is_brought_up_before_whisper():
    """Speech from across the room arrives quiet; Whisper hears it as nothing."""
    far = (np.sin(np.linspace(0, 200, 8000)) * 0.1).astype(np.float32)
    loud = voice.normalize(far)
    assert 0.6 < float(np.max(np.abs(loud))) <= 0.75
    assert np.allclose(loud / float(np.max(np.abs(loud))), far / float(np.max(np.abs(far))), atol=1e-5)


def test_very_faint_audio_is_amplified_but_only_so_far():
    """A 20x ceiling: past that it's mostly room noise, and amplifying it just feeds Whisper hiss."""
    faint = (np.sin(np.linspace(0, 200, 4000)) * 0.02).astype(np.float32)
    assert float(np.max(np.abs(voice.normalize(faint)))) == pytest.approx(0.4, abs=0.01)


def test_normalize_leaves_silence_and_loud_audio_alone():
    hiss = (np.random.default_rng(0).normal(0, 0.0005, 4000)).astype(np.float32)
    assert voice.normalize(hiss) is hiss  # nothing but noise: not amplified
    close = (np.sin(np.linspace(0, 200, 4000)) * 0.9).astype(np.float32)
    assert voice.normalize(close) is close


@pytest.mark.parametrize(
    "heard", ["Hey Solmas", "Hey Sommas", "Somis", "Sommis", "sowmiss", "Samus", "So, miss", "Sonus"]
)
def test_whispers_spellings_of_the_name_all_become_sommus(heard):
    """Real transcripts: the wake phrase only works if these normalise first."""
    assert "Sommus" in voice.NAME_HEARD.sub("Sommus", heard)


@pytest.mark.parametrize("heard", ["summers in Waterloo", "the summers", "some of us"])
def test_words_that_merely_sound_close_do_not_wake_it(heard):
    assert voice.NAME_HEARD.sub("Sommus", heard) == heard
