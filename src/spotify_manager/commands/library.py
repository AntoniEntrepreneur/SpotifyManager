"""Obtain the snapshots: from cache when fresh, from the API otherwise.

Shared by every subcommand that needs the library, so the cache-versus-fetch rule
lives in one place. There are two snapshots -- the saved albums, and Liked Songs --
and `--refresh` bypasses both, so one flag means one idea.

Completing a truncated album's track list also lives here rather than in the planner:
the saved-albums listing embeds only the first page of each album's tracks, and
fetching the rest is I/O. The planner is handed a library whose track lists are
already as complete as they are going to get.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import SNAPSHOT_TTL_SECONDS, Config
from ..infra.auth import TokenProvider
from ..infra.cache import LibraryCache, snapshot_age_seconds
from ..infra.client import SpotifyClient
from ..infra.http import RateLimitedSession
from ..likes.models import AlbumTracks, TrackLibrary, track_from_raw


@dataclass
class LoadResult:
    snapshot: dict[str, Any]
    from_cache: bool
    pages: int
    requests: int
    elapsed_seconds: float
    age_seconds: float


def liked_tracks_cache(config: Config) -> LibraryCache:
    """The Liked Songs cache. Its own file, its own lifetime, its own invalidation."""
    return LibraryCache(config.liked_tracks_snapshot_path, SNAPSHOT_TTL_SECONDS, key="tracks")


def build_client(
    config: Config, *, verbose: bool, rate: float | None = None
) -> SpotifyClient:
    """A client on its own rate-limited session.

    One per phase rather than one per process: the fetch may not have happened at all
    (a fresh snapshot answers from disk), so the phase that needs to write to the API
    asks for its own.

    `rate` overrides the session's client-side requests-per-second ceiling for this
    run. It is an escape hatch for an app whose quota turns out to be tighter than
    the default assumes -- not a second throttle: the session remains the only thing
    in the tool that decides how fast we talk to Spotify.
    """
    session_kwargs: dict[str, Any] = {"verbose": verbose}
    if rate is not None:
        session_kwargs["requests_per_second"] = rate
    return SpotifyClient(RateLimitedSession(TokenProvider(config), **session_kwargs))


def load_library(config: Config, *, refresh: bool, verbose: bool) -> LoadResult:
    cache = LibraryCache(config.snapshot_path, SNAPSHOT_TTL_SECONDS)
    started = time.monotonic()

    if not refresh:
        cached = cache.load()
        if cached is not None:
            return LoadResult(
                snapshot=cached,
                from_cache=True,
                pages=0,
                requests=0,
                elapsed_seconds=time.monotonic() - started,
                age_seconds=snapshot_age_seconds(cached),
            )

    session = RateLimitedSession(TokenProvider(config), verbose=verbose)
    client = SpotifyClient(session)
    items, pages = client.fetch_all_saved_albums()
    snapshot = cache.save(items)

    return LoadResult(
        snapshot=snapshot,
        from_cache=False,
        pages=pages,
        requests=session.stats.requests,
        elapsed_seconds=time.monotonic() - started,
        age_seconds=0.0,
    )


def load_liked_tracks(
    config: Config,
    *,
    refresh: bool,
    verbose: bool,
    client: SpotifyClient | None = None,
) -> LoadResult:
    """The Liked Songs listing, cached exactly like the saved-album one.

    Fetching the whole thing rather than asking `/me/tracks/contains` about each
    album track is the cheaper question by a wide margin once the answer is cached,
    and it is a listing other features can reuse. A client may be passed in so a run
    that is going to write anyway does not open a second session.
    """
    cache = liked_tracks_cache(config)
    started = time.monotonic()

    if not refresh:
        cached = cache.load()
        if cached is not None:
            return LoadResult(
                snapshot=cached,
                from_cache=True,
                pages=0,
                requests=0,
                elapsed_seconds=time.monotonic() - started,
                age_seconds=snapshot_age_seconds(cached),
            )

    if client is None:
        session = RateLimitedSession(TokenProvider(config), verbose=verbose)
        client = SpotifyClient(session)
        requests_before = 0
    else:
        session = client.session
        requests_before = session.stats.requests

    items, pages = client.fetch_all_saved_tracks()
    snapshot = cache.save(items)

    return LoadResult(
        snapshot=snapshot,
        from_cache=False,
        pages=pages,
        requests=session.stats.requests - requests_before,
        elapsed_seconds=time.monotonic() - started,
        age_seconds=0.0,
    )


def complete_album_tracks(
    library: TrackLibrary,
    client: SpotifyClient,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> TrackLibrary:
    """Fetch the rest of the tracks for every album whose listed page was truncated.

    The saved-albums listing embeds 50 tracks per album, which on a real library is
    every track of all but a handful of them. Those few are completed one request at
    a time here.

    An album whose completion fails keeps the tracks it already had and stays marked
    truncated, so the plan reports it as incomplete rather than pretending a 60-track
    album has 50 tracks. One unreachable album never costs the run the other 1,280.
    """

    def say(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    completed: list[AlbumTracks] = []
    for entry in library.albums:
        if not entry.truncated:
            completed.append(entry)
            continue
        say(f"Completing the track list for {entry.album.name!r}.")
        try:
            raw_tracks = client.fetch_album_tracks(entry.album.id)
        except Exception as exc:  # noqa: BLE001 - one album, not the run
            say(
                f"Could not fetch the full track list for {entry.album.name!r} "
                f"({type(exc).__name__}: {exc}). Continuing without it."
            )
            completed.append(entry)
            continue
        tracks = tuple(track_from_raw(t) for t in raw_tracks if isinstance(t, dict))
        completed.append(
            AlbumTracks(
                album=entry.album,
                tracks=tracks or entry.tracks,
                truncated=not tracks,
            )
        )
    return TrackLibrary(fetched_at=library.fetched_at, albums=tuple(completed))
