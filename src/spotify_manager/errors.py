"""Project-level error types.

Every error raised deliberately by this package derives from `SpotifyManagerError`.
The CLI entry point catches it and prints `str(exc)` to stderr with exit code 1 --
never a traceback. Anything that escapes as a traceback is therefore a genuine bug,
not a user misconfiguration.
"""


class SpotifyManagerError(Exception):
    """Base class for all deliberate, user-facing failures."""


class ConfigError(SpotifyManagerError):
    """Configuration is missing or wrong; the message names exactly what to fix."""


class ApiError(SpotifyManagerError):
    """The Spotify Web API returned a response we cannot proceed from."""
