"""Google OAuth for the Gmail node.

`credentials.json` is the OAuth client downloaded from Google Cloud; `gmail_token.json`
is the account's own grant, written on first sign-in. Both are gitignored — the token
is as good as a password for the mailbox.
"""

from __future__ import annotations

from pathlib import Path

from sommus.config import ROOT

# modify covers reading, labelling and drafting; send covers sending. Neither can delete.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

CLIENT_SECRET = ROOT / "credentials.json"
TOKEN = ROOT / "gmail_token.json"

SETUP_HELP = (
    "Gmail isn't connected yet. Put the OAuth client file from Google Cloud at "
    f"{CLIENT_SECRET.name} in the Sommus folder, then run: uv run sommus gmail-auth"
)


class GmailAuthError(Exception):
    """Setup or sign-in problem, phrased for the user."""


def load_credentials(interactive: bool = False):
    """Return usable credentials, refreshing or re-authorising as needed."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked, or the 7-day testing-mode expiry — sign in again

    if not creds or not creds.valid:
        if not interactive:
            raise GmailAuthError(
                SETUP_HELP if not TOKEN.exists() else "Gmail sign-in expired. Run: uv run sommus gmail-auth"
            )
        if not CLIENT_SECRET.exists():
            raise GmailAuthError(SETUP_HELP)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        TOKEN.write_text(creds.to_json())
        TOKEN.chmod(0o600)

    return creds


def service(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=load_credentials(interactive), cache_discovery=False)


def sign_in() -> str:
    """Run the browser consent flow and report which account was connected."""
    api = service(interactive=True)
    return api.users().getProfile(userId="me").execute().get("emailAddress", "unknown")


def token_path_note() -> str:
    return f"Token stored at {TOKEN}"


def credentials_present() -> bool:
    return CLIENT_SECRET.exists()


def signed_in() -> bool:
    return TOKEN.exists()


def paths() -> tuple[Path, Path]:
    return CLIENT_SECRET, TOKEN
