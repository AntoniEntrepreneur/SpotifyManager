"""Environment configuration and local state-directory resolution.

Credentials come from the environment, loaded from a `.env` file in the current
working directory (or any parent) if one exists. Nothing here reads the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .errors import ConfigError

#: OAuth scopes requested at login. Read is needed for the library fetch; modify is
#: requested at the same time so the eventual deletion path never needs a second login.
SCOPES = "user-library-read user-library-modify"

#: Directory holding all local runtime state (token cache, snapshot, ledger, reports).
#: Project-local rather than XDG so a checkout is self-contained; gitignored.
STATE_DIR_NAME = ".spotifymanager"

#: How long a library snapshot stays usable before it is refetched. Six hours keeps
#: repeated runs in one sitting free while never operating on a day-old picture.
SNAPSHOT_TTL_SECONDS = 6 * 60 * 60

_REQUIRED_VARS = (
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
    "SPOTIPY_REDIRECT_URI",
)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    state_dir: Path

    @property
    def token_cache_path(self) -> Path:
        return self.state_dir / "token_cache.json"

    @property
    def snapshot_path(self) -> Path:
        return self.state_dir / "library_snapshot.json"

    @property
    def restores_dir(self) -> Path:
        """Where every run that deletes something writes its restore file first.

        Separate from the reports directory on purpose: a report is a record you may
        delete when the disk fills up, and a restore file is the only way back from
        an approved mistake. They should not be confusable.
        """
        return self.state_dir / "restores"

    @property
    def reports_dir(self) -> Path:
        """Where every run archives its rendered report. Local state, gitignored."""
        return self.state_dir / "reports"


def state_dir() -> Path:
    """Return the local state directory, creating it if needed."""
    path = Path.cwd() / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config() -> Config:
    """Load credentials from the environment (and `.env`).

    Raises:
        ConfigError: with a message naming exactly which variables are missing and
            where to set them. Never a traceback for the ordinary "I haven't set this
            up yet" case.
    """
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path)

    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(_missing_vars_message(missing, dotenv_path))

    redirect_uri = os.environ["SPOTIPY_REDIRECT_URI"].strip()
    if not redirect_uri.startswith(("http://", "https://")):
        raise ConfigError(
            f"SPOTIPY_REDIRECT_URI is set to {redirect_uri!r}, which is not a URL.\n"
            "It must be the full redirect URI you registered in your Spotify app, "
            "for example http://127.0.0.1:8888/callback"
        )

    return Config(
        client_id=os.environ["SPOTIPY_CLIENT_ID"].strip(),
        client_secret=os.environ["SPOTIPY_CLIENT_SECRET"].strip(),
        redirect_uri=redirect_uri,
        state_dir=state_dir(),
    )


def _missing_vars_message(missing: list[str], dotenv_path: str) -> str:
    where = dotenv_path if dotenv_path else str(Path.cwd() / ".env")
    lines = [
        "Spotify credentials are not configured.",
        "",
        "Missing: " + ", ".join(missing),
        "",
        f"Fix: add the following to {where} (or export them in your shell):",
    ]
    example = {
        "SPOTIPY_CLIENT_ID": "<the Client ID from your Spotify app>",
        "SPOTIPY_CLIENT_SECRET": "<the Client secret from your Spotify app>",
        "SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    }
    lines += [f"  {name}={example[name]}" for name in missing]
    lines += [
        "",
        "Client ID and secret come from https://developer.spotify.com/dashboard "
        "(open your app, then Settings).",
        "The redirect URI must be added to that same app under Settings -> Redirect URIs "
        "and must match the value above character for character.",
    ]
    return "\n".join(lines)
