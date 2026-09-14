"""Gmail node: message parsing and reply construction (no network)."""

import email
from email.message import EmailMessage

import pytest

from sommus.nodes.gmail import mail


def message_from(text: str) -> email.message.Message:
    return email.message_from_string(text)


def test_plain_text_body_is_preferred_over_html():
    raw = EmailMessage()
    raw["Subject"] = "Hi"
    raw.set_content("the plain part")
    raw.add_alternative("<p>the <b>html</b> part</p>", subtype="html")
    assert mail._body(email.message_from_bytes(raw.as_bytes())).strip() == "the plain part"


def test_html_only_body_is_stripped_of_tags_and_scripts():
    raw = EmailMessage()
    raw["Subject"] = "Hi"
    raw.add_alternative("<style>p{color:red}</style><p>Meeting at <b>4:30</b></p>", subtype="html")
    body = mail._body(email.message_from_bytes(raw.as_bytes()))
    assert "Meeting at" in body and "4:30" in body
    assert "<p>" not in body and "color:red" not in body


def test_encoded_headers_are_decoded():
    assert mail.decode_header_value("=?utf-8?q?Caf=C3=A9_meeting?=") == "Café meeting"
    assert mail.decode_header_value(None) == ""


def test_reply_keeps_the_thread_and_prefixes_the_subject():
    original = message_from(
        "From: prof@uwaterloo.ca\nTo: me@gmail.com\nSubject: Lab 2\nMessage-ID: <abc@mail>\n\nhello"
    )
    reply = mail.build_reply(original, "on my way", reply_all=False)
    assert reply["To"] == "prof@uwaterloo.ca"
    assert reply["Subject"] == "Re: Lab 2"
    assert reply["In-Reply-To"] == "<abc@mail>" and "<abc@mail>" in reply["References"]


def test_reply_does_not_re_prefix_an_existing_re_subject():
    original = message_from("From: a@b.c\nSubject: Re: Lab 2\nMessage-ID: <x@y>\n\nhi")
    assert mail.build_reply(original, "ok", reply_all=False)["Subject"] == "Re: Lab 2"


def test_reply_all_drops_the_users_own_address_from_cc(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    original = message_from("From: a@b.c\nCc: me@gmail.com, friend@x.com\nSubject: Plans\nMessage-ID: <x@y>\n\nhi")
    assert mail.build_reply(original, "ok", reply_all=True)["Cc"] == "friend@x.com"


def test_missing_credentials_explain_the_setup(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert not mail.configured()
    with pytest.raises(mail.MailError, match="app password"):
        mail.account()


def test_app_password_spaces_are_ignored(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    assert mail.account() == ("me@gmail.com", "abcdefghijklmnop")


def test_send_sets_the_headers_and_hands_the_message_to_smtp(monkeypatch):
    """Regression: EmailMessage has no .setdefault, which made every send fail."""
    sent = {}

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, address, password):
            sent["login"] = (address, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", lambda *a, **k: FakeSMTP())

    message = EmailMessage()
    message["To"] = "friend@x.com"
    message["Subject"] = "Running late"
    message.set_content("on my way")
    mail.send(message)

    assert sent["login"] == ("me@gmail.com", "abcdefghijklmnop")
    assert sent["message"]["From"] == "me@gmail.com"
    assert sent["message"]["Message-ID"] and sent["message"]["Date"]
