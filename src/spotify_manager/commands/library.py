"""Obtain the library snapshot: from cache when fresh, from the API otherwise.

Shared by every subcommand that needs the library, so the cache-versus-fetch rule
lives in one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import SNAPSHOT_TTL_SECONDS, Config
from ..infra.auth import TokenProvider
from ..infra.cache import LibraryCache, snapshot_age_seconds
from ..infra.client import SpotifyClient
from ..infra.http import RateLimitedSession


@dataclass
class LoadResult:
    snapshot: dict[str, Any]
    from_cache: bool
    pages: int
    requests: int
    elapsed_seconds: float
    age_seconds: float


def build_client(config: Config, *, verbose: bool) -> SpotifyClient:
    """A client on its own rate-limited session.

    One per phase rather than one per process: the fetch may not have happened at all
    (a fresh snapshot answers from disk), so the phase that needs to write to the API
    asks for its own.
    """
    return SpotifyClient(RateLimitedSession(TokenProvider(config), verbose=verbose))


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
