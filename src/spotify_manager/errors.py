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
    """The Spotify Web API returned a response we cannot proceed from.

    `status` carries the HTTP status code when the failure arrived as a response, and
    is None when there was no response to read one from (a timeout, a dropped
    connection, a malformed reply). The distinction matters downstream: a 4xx is
    Spotify rejecting a request outright, which is proof that the write did not
    happen, whereas an error with no status may have applied before it failed.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
