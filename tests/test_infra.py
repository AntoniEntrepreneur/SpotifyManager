"""Unit tests for the genuinely pure parts of the I/O shell.

Per the spec's Testing Decisions, auth, the HTTP session and the cache are otherwise
untested I/O shell, verified by running the tool read-only against the real account.
The Spotify API is never mocked. What is tested here is the arithmetic and data
shaping that a mock would not exercise anyway: cache freshness, retry delays, and
fixture redaction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from spotify_manager.infra.cache import LibraryCache, is_fresh
from spotify_manager.infra.http import (
    RETRY_AFTER_FALLBACK_SECONDS,
    backoff_delay,
    parse_retry_after,
)
from spotify_manager.tools.redact import redact_snapshot

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot(age: timedelta, albums=()):
    return {"fetched_at": (NOW - age).isoformat(), "albums": list(albums)}


# -- cache time-to-live ------------------------------------------------------


def test_snapshot_within_ttl_is_fresh():
    assert is_fresh(_snapshot(timedelta(hours=1)), ttl_seconds=6 * 3600, now=NOW)


def test_snapshot_past_ttl_expires_on_its_own():
    assert not is_fresh(_snapshot(timedelta(hours=7)), ttl_seconds=6 * 3600, now=NOW)


def test_snapshot_exactly_at_ttl_is_expired():
    assert not is_fresh(_snapshot(timedelta(hours=6)), ttl_seconds=6 * 3600, now=NOW)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"albums": []},
        {"fetched_at": "not a timestamp", "albums": []},
        {"fetched_at": (NOW + timedelta(hours=1)).isoformat(), "albums": []},
    ],
)
def test_unusable_timestamps_are_treated_as_stale(snapshot):
    assert not is_fresh(snapshot, ttl_seconds=6 * 3600, now=NOW)


def test_cache_reads_back_what_it_wrote(tmp_path):
    cache = LibraryCache(tmp_path / "snap.json", ttl_seconds=3600)
    cache.save([{"album": {"id": "abc"}}])
    loaded = cache.load()
    assert loaded is not None
    assert loaded["albums"] == [{"album": {"id": "abc"}}]


def test_cache_misses_when_file_absent(tmp_path):
    assert LibraryCache(tmp_path / "missing.json", ttl_seconds=3600).load() is None


def test_cache_misses_when_expired(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_snapshot(timedelta(days=1))), encoding="utf-8")
    assert LibraryCache(path, ttl_seconds=3600).load(now=NOW) is None


def test_corrupt_cache_is_a_miss_not_a_failure(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text("{ not json", encoding="utf-8")
    assert LibraryCache(path, ttl_seconds=3600).load() is None


# -- retry timing ------------------------------------------------------------


def test_backoff_doubles_each_attempt():
    assert [backoff_delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped():
    assert backoff_delay(20, base=1.0, cap=30.0) == 30.0


def test_backoff_jitter_never_shortens_the_wait():
    for _ in range(50):
        delay = backoff_delay(2, jitter=0.25)
        assert 2.0 <= delay <= 2.5


def test_retry_after_header_is_honoured_exactly():
    assert parse_retry_after("7") == 7.0


@pytest.mark.parametrize("value", [None, "", "soon", "-3"])
def test_missing_or_unusable_retry_after_falls_back(value):
    assert parse_retry_after(value) == RETRY_AFTER_FALLBACK_SECONDS


# -- fixture redaction -------------------------------------------------------

RAW_ITEM = {
    "added_at": "2023-04-05T18:22:31Z",
    "album": {
        "id": "alb1",
        "name": "Abbey Road (Super Deluxe Edition)",
        "album_type": "album",
        "release_date": "2019-09-27",
        "release_date_precision": "day",
        "total_tracks": 40,
        "available_markets": ["GB", "US"],
        "href": "https://api.spotify.com/v1/albums/alb1",
        "uri": "spotify:album:alb1",
        "external_urls": {"spotify": "https://open.spotify.com/album/alb1"},
        "external_ids": {"upc": "00000000"},
        "artists": [
            {
                "id": "art1",
                "name": "The Beatles",
                "href": "https://api.spotify.com/v1/artists/art1",
                "uri": "spotify:artist:art1",
                "external_urls": {"spotify": "https://open.spotify.com/artist/art1"},
            }
        ],
        "images": [
            {"url": "https://i.scdn.co/image/big", "height": 640, "width": 640, "extra": 1}
        ],
    },
}


def test_redaction_keeps_everything_the_planner_needs():
    album = redact_snapshot({"fetched_at": "x", "albums": [RAW_ITEM]})["albums"][0]["album"]
    assert album["id"] == "alb1"
    assert album["name"] == "Abbey Road (Super Deluxe Edition)"
    assert album["album_type"] == "album"
    assert album["release_date"] == "2019-09-27"
    assert album["release_date_precision"] == "day"
    assert album["total_tracks"] == 40
    assert album["artists"] == [{"id": "art1", "name": "The Beatles"}]
    assert album["images"] == [
        {"url": "https://i.scdn.co/image/big", "height": 640, "width": 640}
    ]


def test_redaction_reduces_added_at_to_a_date():
    item = redact_snapshot({"albums": [RAW_ITEM]})["albums"][0]
    assert item["added_at"] == "2023-04-05"


def test_redaction_drops_account_and_plumbing_fields():
    redacted = redact_snapshot({"fetched_at": "2026-01-01T00:00:00+00:00", "albums": [RAW_ITEM]})
    assert redacted["fetched_at"] == "redacted"
    blob = json.dumps(redacted)
    for leaked in ("available_markets", "href", "uri", "external_urls", "external_ids", "extra"):
        assert leaked not in blob


def test_redaction_is_deterministic():
    snapshot = {"fetched_at": "2026-01-01T00:00:00+00:00", "albums": [RAW_ITEM]}
    assert json.dumps(redact_snapshot(snapshot)) == json.dumps(redact_snapshot(snapshot))


def test_redaction_survives_a_malformed_item():
    redacted = redact_snapshot({"albums": [{"album": {"id": "x"}}]})
    album = redacted["albums"][0]["album"]
    assert redacted["albums"][0]["added_at"] is None
    assert album["artists"] == [] and album["images"] == []
