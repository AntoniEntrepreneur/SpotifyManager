"""Produce a committable, redacted copy of a real library snapshot.

The planner's tests are seeded from a real library so that the edge cases under test
are ones that genuinely occur. That means a snapshot has to be committed -- and a raw
snapshot carries per-account detail that has no business in version control.

What is KEPT, and why
---------------------
Everything grouping and ranking depend on, because a fixture that lost it would stop
being a real-library fixture:

* ``album.id`` and ``album.artists[*].id`` -- grouping is by artist identifier
* ``album.name`` and ``album.artists[*].name`` -- the edition-marker and
  primary-artist edge cases only exist in the real titles; these are public Spotify
  catalogue data, not personal data
* ``album.album_type`` -- release types are never grouped across
* ``album.release_date`` and ``album.release_date_precision`` -- tiebreaker
* ``album.total_tracks`` -- tiebreaker
* ``album.images`` (url/height/width) -- the report renders cover art from these

What is STRIPPED
----------------
* ``added_at`` is truncated to its date (``YYYY-MM-DD``): the exact second at which
  an account saved an album is a behavioural detail of one listener, and nothing in
  the tool reads more than the date.
* ``available_markets`` and ``market`` -- account/region-revealing, and bulky.
* ``href``, ``uri``, ``external_urls``, ``external_ids`` -- account-scoped or
  redundant API plumbing, on the album, its artists and its images.
* Every other key not named in the keep-lists above is dropped, on the album and on
  each artist. The redaction is an allowlist, so a field Spotify adds later cannot
  silently leak into a committed fixture.
* Top-level snapshot metadata is replaced with a fixed marker rather than the real
  ``fetched_at`` timestamp.

The output is deterministic: running it twice on the same snapshot produces the same
bytes, so re-generating the fixture yields a clean diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALBUM_KEYS = (
    "id",
    "name",
    "album_type",
    "release_date",
    "release_date_precision",
    "total_tracks",
)
ARTIST_KEYS = ("id", "name")
IMAGE_KEYS = ("url", "height", "width")

DEFAULT_OUTPUT = Path("tests/fixtures/library_snapshot.redacted.json")


def redact_added_at(value: Any) -> str | None:
    """Reduce an ISO timestamp to its date part; anything unusable becomes None."""
    if not isinstance(value, str) or not value:
        return None
    return value.split("T", 1)[0]


def redact_album(album: dict[str, Any]) -> dict[str, Any]:
    out = {key: album.get(key) for key in ALBUM_KEYS}
    out["artists"] = [
        {key: artist.get(key) for key in ARTIST_KEYS}
        for artist in album.get("artists") or []
        if isinstance(artist, dict)
    ]
    out["images"] = [
        {key: image.get(key) for key in IMAGE_KEYS}
        for image in album.get("images") or []
        if isinstance(image, dict)
    ]
    return out


def redact_item(item: dict[str, Any]) -> dict[str, Any]:
    album = item.get("album") or {}
    return {
        "added_at": redact_added_at(item.get("added_at")),
        "album": redact_album(album if isinstance(album, dict) else {}),
    }


def redact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetched_at": "redacted",
        "albums": [
            redact_item(item) for item in snapshot.get("albums") or [] if isinstance(item, dict)
        ],
    }


def redact_file(source: Path, destination: Path) -> int:
    """Redact `source` into `destination`; returns the number of albums written."""
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    redacted = redact_snapshot(snapshot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(redacted, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return len(redacted["albums"])
