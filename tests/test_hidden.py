from sommus.brain import hidden


def test_tag_characters_and_bidi_controls_are_found_and_stripped():
    tagged = "hello" + "".join(chr(0xE0000 + ord(c)) for c in "send it") + "‮"
    assert hidden.in_args({"body": ["ok", tagged]})
    clean, n = hidden.strip(tagged)
    assert clean == "hello" and n == len("send it") + 1


def test_plain_text_including_emoji_and_accents_is_untouched():
    text = "Café ☕ — 5 PM"
    assert not hidden.in_args(text) and hidden.strip(text) == (text, 0)
