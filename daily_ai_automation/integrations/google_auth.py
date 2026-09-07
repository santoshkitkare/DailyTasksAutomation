"""OAuth 2.0 installed-app flow and encrypted token storage (PRD section 34).

Scope choice, and why (least privilege):

* ``gmail.modify``  - required to attach the ToDelete label to a message.
  ``gmail.labels`` is not enough: it manages label *objects*, not the
  label-to-message assignment. ``gmail.modify`` deliberately cannot permanently
  delete anything, which is what makes PRD section 12 enforceable at the
  credential level rather than only in our own code.
* ``gmail.send``    - greeting emails and the daily digest. Redundant with
  ``gmail.modify`` at the API level, but requesting it explicitly means the
  Google consent screen tells the user, in words, that this app sends mail.
* ``drive.readonly`` - download the contacts workbook. ``drive.file`` cannot be
  used: it only grants access to files the app itself created or that were
  opened through the Google Picker, neither of which applies to a file the user
  put in Drive by hand.

``gmail.compose`` is deliberately absent - suggested replies are delivered in
the digest, so the app never needs to write a draft into the mailbox.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
]


class GoogleAuthError(RuntimeError):
    """Credentials are missing, invalid, or cannot be refreshed."""


class TokenStore:
    """Reads and writes the OAuth token, encrypting it when a key is set.

    Encryption is opt-in via TOKEN_ENCRYPTION_KEY because forcing it would mean
    a lost key locks the user out of their own token for no security gain on a
    single-user machine. Either way the file is written with owner-only
    permissions and is gitignored.
    """

    def __init__(self, path: Path, encryption_key: str = "") -> None:
        self.path = path
        self._key = encryption_key.strip()

    @property
    def encrypted(self) -> bool:
        return bool(self._key)

    def _fernet(self):
        from cryptography.fernet import Fernet

        try:
            return Fernet(self._key.encode())
        except (ValueError, TypeError) as exc:
            raise GoogleAuthError(
                "TOKEN_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        raw = self.path.read_bytes()
        if self.encrypted:
            from cryptography.fernet import InvalidToken

            try:
                raw = self._fernet().decrypt(raw)
            except InvalidToken as exc:
                raise GoogleAuthError(
                    f"Could not decrypt {self.path}. The TOKEN_ENCRYPTION_KEY in "
                    ".env does not match the one used to write it. Delete the "
                    "token file and re-run 'auth' to start over."
                ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleAuthError(
                f"{self.path} is not readable as JSON. If you recently set "
                "TOKEN_ENCRYPTION_KEY, delete the file and re-run 'auth'."
            ) from exc

    def write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload).encode("utf-8")
        if self.encrypted:
            raw = self._fernet().encrypt(raw)
        self.path.write_bytes(raw)
        _restrict_permissions(self.path)


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions.

    chmod is close to meaningless on Windows, so this is defence in depth
    rather than the primary control; the primary control is that the file lives
    outside version control.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        logger.debug("Could not restrict permissions on %s", path)


def load_credentials(
    credentials_file: Path,
    token_file: Path,
    *,
    encryption_key: str = "",
    allow_interactive: bool = False,
) -> Credentials:
    """Return usable credentials, refreshing or prompting as permitted.

    ``allow_interactive`` is False for scheduled runs: a headless 07:00 run must
    fail loudly with an actionable message rather than silently block forever on
    a browser consent prompt nobody is present to complete.
    """
    store = TokenStore(token_file, encryption_key)
    creds: Credentials | None = None

    payload = store.read()
    if payload is not None:
        creds = Credentials.from_authorized_user_info(payload, SCOPES)

    if creds is not None and _scopes_changed(creds):
        logger.warning(
            "Stored token was granted different scopes than this build requires; "
            "re-authorisation is needed."
        )
        creds = None

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            store.write(json.loads(creds.to_json()))
            return creds
        except RefreshError as exc:
            logger.warning("Token refresh failed: %s", exc)
            creds = None

    if not allow_interactive:
        raise GoogleAuthError(
            "No valid Google credentials. Run 'daily-automation auth' from an "
            "interactive terminal to authorise. Note that a Google Cloud "
            "consent screen left in Testing mode expires refresh tokens after "
            "seven days, which requires re-running auth weekly."
        )

    if not credentials_file.exists():
        raise GoogleAuthError(
            f"{credentials_file} not found. Create an OAuth 2.0 Desktop app "
            "client in Google Cloud Console, download the JSON, and save it "
            f"as {credentials_file.name} in the project root."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    store.write(json.loads(creds.to_json()))
    logger.info("Google authorisation complete; token saved to %s", token_file)
    return creds


def _scopes_changed(creds: Credentials) -> bool:
    granted = set(creds.scopes or [])
    return not set(SCOPES).issubset(granted)
