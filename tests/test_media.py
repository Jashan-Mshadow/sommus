from sommus.interfaces.media import MediaState, audio_out_pids

# Shape taken from `pmset -g assertions` on the Mac while afplay was playing (2026-11-18).
PLAYING = """Listed by owning process:
   pid 1064(coreaudiod): [0x0005784d00019ee4] 00:00:01 PreventUserIdleSystemSleep named: "com.apple.audio.out"
\tCreated for PID: 17765.
\tResources: audio-out 10-94-97-69-84-E0:output
   pid 1064(coreaudiod): [0x0005784d00019ee5] 00:12:06 PreventUserIdleSystemSleep named: "com.apple.audio.tap"
\tCreated for PID: 11359.
\tResources: audio-in 3382144D-5DC3-4230-8F3B-2EA1C10F7D19
   pid 994(powerd): [0x00005342000980dd] 11:36:10 UserIsActive named: "com.apple.powermanagement.kernel.useractive"
"""


def test_audio_out_pids_picks_outputs_only():
    assert audio_out_pids(PLAYING) == {17765}  # 11359 is a microphone tap, not sound out


def test_audio_out_pids_empty_when_silent():
    silent = PLAYING.split("pid 1064")[0] + '   pid 994(powerd): [0x1] 1:00 UserIsActive named: "x"\n'
    assert audio_out_pids(silent) == set()


def test_a_process_holding_mic_and_speaker_together_is_not_media():
    siri = "\tCreated for PID: 1460.\n\tResources: audio-in audio-out BuiltInSpeakerDevice\n"
    assert audio_out_pids(siri) == set()  # corespeechd, always on
    assert audio_out_pids(siri + "\tCreated for PID: 7.\n\tResources: audio-out BuiltInSpeakerDevice\n") == {7}


def test_input_resource_does_not_inherit_a_pid_from_an_earlier_output():
    text = "\tCreated for PID: 5.\n\tResources: audio-in A\n\tResources: audio-out B\n"
    assert audio_out_pids(text) == set()


def test_a_chime_does_not_count_as_media():
    state = MediaState(on_after=2, off_after=5)
    assert [state.feed(x) for x in (True, False, False)] == [False, False, False]


def test_sustained_sound_switches_on_and_a_pause_does_not_switch_off():
    state = MediaState(on_after=2, off_after=5)
    assert [state.feed(True) for _ in range(3)] == [False, True, True]
    assert [state.feed(False) for _ in range(4)] == [True, True, True, True]  # a pause
    assert state.feed(True) is True  # sound is back: still media
    assert [state.feed(False) for _ in range(5)] == [True, True, True, True, False]  # really over
