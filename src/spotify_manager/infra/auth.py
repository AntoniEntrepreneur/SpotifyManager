"""Browser-based OAuth, using spotipy purely for the authorization-code dance.

spotipy owns the parts it is uniquely good at -- opening the browser, running the
short-lived local listener on the redirect URI, exchanging the code, and caching and
silently refreshing the token. Every actual Web API call goes through
`infra.http.RateLimitedSession` instead, so that throttling, retries and verbose
logging live in exactly one place.
"""

from __future__ import annotations

from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from ..config import SCOPES, Config
from ..errors import ConfigError


class TokenProvider:
    """Supplies a fresh bearer token, refreshing silently when it has expired."""

    def __init__(self, config: Config) -> None:
        self._config = config
        cache_handler = CacheFileHandler(cache_path=str(config.token_cache_path))
        try:
            self._auth_manager = SpotifyOAuth(
                client_id=config.client_id,
                client_secret=config.client_secret,
                redirect_uri=config.redirect_uri,
                scope=SCOPES,
                cache_handler=cache_handler,
                open_browser=True,
            )
        except SpotifyOauthError as exc:
            raise ConfigError(_oauth_message(config, exc)) from exc

    @property
    def auth_manager(self) -> SpotifyOAuth:
        return self._auth_manager

    def access_token(self) -> str:
        """Return a valid access token.

        On first run this opens the browser and blocks until login completes; the
        token is then cached, so later runs return immediately (refreshing in the
        background when the cached token has expired).
        """
        try:
            token = self._auth_manager.get_access_token(as_dict=False)
        except SpotifyOauthError as exc:
            raise ConfigError(_oauth_message(self._config, exc)) from exc
        if not token:
            raise ConfigError(
                "Spotify did not return an access token. Log in again by deleting "
                f"{self._config.token_cache_path} and re-running the command."
            )
        return token


def _oauth_message(config: Config, exc: Exception) -> str:
    return (
        "Spotify rejected the login configuration.\n"
        "\n"
        f"Spotify said: {exc}\n"
        "\n"
        "Check, in https://developer.spotify.com/dashboard -> your app -> Settings:\n"
        "  * SPOTIPY_CLIENT_ID matches the app's Client ID\n"
        "  * SPOTIPY_CLIENT_SECRET matches the app's Client secret\n"
        f"  * the app's Redirect URIs list contains exactly {config.redirect_uri}\n"
        "\n"
        f"If the credentials changed, delete {config.token_cache_path} and log in again."
    )
