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
from spotify_manager.infra.client import SpotifyClient
from spotify_manager.infra.http import (
    RETRY_AFTER_FALLBACK_SECONDS,
    RateLimitedSession,
    _client_error_message,
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
        {"fetched_at": 1700000000, "albums": []},
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


def test_a_cache_can_hold_a_different_listing_under_a_different_key(tmp_path):
    """One class, two snapshots: saved albums and liked tracks."""
    cache = LibraryCache(tmp_path / "liked.json", 3600, key="tracks")
    saved = cache.save([{"track": {"id": "t1"}}])

    assert "tracks" in saved and "albums" not in saved
    assert cache.load()["tracks"] == [{"track": {"id": "t1"}}]


def test_a_snapshot_written_under_one_key_is_not_read_under_another(tmp_path):
    """A misread cache would silently answer the wrong question."""
    path = tmp_path / "snapshot.json"
    LibraryCache(path, 3600, key="albums").save([{"album": {"id": "a1"}}])

    assert LibraryCache(path, 3600, key="tracks").load() is None


def test_discarding_a_snapshot_removes_it(tmp_path):
    cache = LibraryCache(tmp_path / "liked.json", 3600, key="tracks")
    cache.save([])

    assert cache.discard() is True
    assert cache.load() is None


def test_discarding_a_snapshot_that_is_not_there_is_harmless(tmp_path):
    assert LibraryCache(tmp_path / "missing.json", 3600).discard() is False


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


# -- what a client error tells the user --------------------------------------


class FakeResponse:
    """Just enough of `requests.Response` for the message builder to read."""

    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _forbidden(message: str) -> str:
    response = FakeResponse(403, {"error": {"status": 403, "message": message}})
    return _client_error_message("DELETE", "https://api.spotify.com/v1/me/albums", response)


def test_a_missing_scope_403_says_to_log_in_again():
    text = _forbidden("Insufficient client scope")

    assert "Insufficient client scope" in text, "Spotify's own words, verbatim"
    assert "token_cache.json" in text
    assert "User Management" not in text


def test_a_bare_forbidden_403_names_every_likely_cause_and_asserts_none():
    """The one that cost real debugging time -- twice, for opposite reasons.

    Spotify says only "Forbidden", so the message must not pretend to know which of
    several unrelated problems it is. A retired endpoint and an unlisted account both
    look exactly like this, and confidently naming either one sends the reader off to
    fix something that is not broken.
    """
    text = _forbidden("Forbidden")

    assert "Forbidden" in text, "Spotify's own words, verbatim"
    # The deprecation, which is what this actually was.
    assert "february-2026" in text
    assert "/v1/me/library" in text
    # And the allowlist, which it can equally be.
    assert "User Management" in text
    assert "Development Mode" in text
    assert "quota-modes" in text
    # Deleting the token cache does not help here, so it must not be suggested.
    assert "token_cache.json" not in text


def test_a_bare_forbidden_403_quotes_the_request_that_got_it():
    """Which endpoint was called is the reader's main clue between the causes."""
    text = _forbidden("Forbidden")

    assert "DELETE https://api.spotify.com/v1/me/albums" in text


def test_a_bare_forbidden_403_claims_no_single_cause():
    """A phrasing guard: the old message asserted the allowlist as fact."""
    text = _forbidden("Forbidden")

    assert "the account you logged in with is not authorised" not in text


def test_the_two_403_bodies_get_different_guidance():
    assert _forbidden("Insufficient client scope") != _forbidden("Forbidden")


def test_a_401_still_says_to_log_in_again():
    response = FakeResponse(401, {"error": {"status": 401, "message": "expired"}})
    text = _client_error_message("GET", "https://api.spotify.com/v1/me/albums", response)

    assert "401" in text and "token_cache.json" in text


def test_any_other_client_error_quotes_the_method_url_and_message():
    response = FakeResponse(400, {"error": {"status": 400, "message": "bad id"}})
    text = _client_error_message("PUT", "https://api.spotify.com/v1/me/albums", response)

    assert "400" in text and "PUT" in text and "bad id" in text


# -- library writes ----------------------------------------------------------
#
# Spotify's February 2026 changes retired the per-entity library writes. Everything
# below pins the shape of the replacement, because the endpoint it replaced answered
# a bare 403 rather than saying anything about deprecation: a regression here would
# come back as an error message pointing at the wrong thing entirely.


class _OkResponse:
    status_code = 200
    headers: dict[str, str] = {}
    content = b""
    text = ""

    def json(self) -> dict:
        return {}


class _RecordingHttp:
    """Stands in for `requests.Session` inside the real `RateLimitedSession`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, object]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        return _OkResponse()


class _Token:
    def access_token(self) -> str:
        return "token"


def _recorded(write, ids):
    """Run one client write through the real session, and return what it sent."""
    session = RateLimitedSession(_Token(), requests_per_second=0)
    http = _RecordingHttp()
    session._session = http
    write(SpotifyClient(session))(ids)
    return http.calls


def test_a_library_write_goes_to_the_one_endpoint_that_replaced_the_retired_ones():
    calls = _recorded(lambda c: c.delete_albums, ["a1", "a2"])

    assert [(m, u) for m, u, _p, _j in calls] == [
        ("DELETE", "https://api.spotify.com/v1/me/library")
    ]


def test_ids_are_sent_as_uris_in_a_query_parameter_not_a_json_body():
    """Verified against the live API: a JSON body is answered `Missing required
    field: uris`, so the URIs must travel in the query string, comma-separated."""
    (_method, _url, params, body), = _recorded(lambda c: c.delete_albums, ["a1", "a2"])

    assert params == {"uris": "spotify:album:a1,spotify:album:a2"}
    assert body is None


def test_albums_and_tracks_each_get_their_own_uri_prefix():
    (_m, _u, saved, _j), = _recorded(lambda c: c.save_albums, ["a1"])
    (_m2, _u2, liked, _j2), = _recorded(lambda c: c.save_tracks, ["t1"])

    assert saved == {"uris": "spotify:album:a1"}
    assert liked == {"uris": "spotify:track:t1"}


def test_saving_uses_put_and_removing_uses_delete():
    assert _recorded(lambda c: c.save_albums, ["a1"])[0][0] == "PUT"
    assert _recorded(lambda c: c.save_tracks, ["t1"])[0][0] == "PUT"
    assert _recorded(lambda c: c.delete_albums, ["a1"])[0][0] == "DELETE"


def test_no_request_carries_more_uris_than_the_endpoint_accepts():
    """41 is answered `400 Too many uris requested`, so 40 is the whole budget."""
    assert ID_BATCH_LIMIT == 40

    calls = _recorded(lambda c: c.delete_albums, [f"a{i}" for i in range(81)])

    sizes = [len(params["uris"].split(",")) for _m, _u, params, _j in calls]
    assert sizes == [40, 40, 1]


def test_exactly_forty_ids_still_go_out_in_one_request():
    """The boundary itself: 40 is allowed, so splitting it would waste a request."""
    calls = _recorded(lambda c: c.save_albums, [f"a{i}" for i in range(40)])

    assert len(calls) == 1
    assert len(calls[0][2]["uris"].split(",")) == 40


def test_a_chunked_write_loses_nothing_and_keeps_its_order():
    ids = [f"a{i:03d}" for i in range(95)]
    calls = _recorded(lambda c: c.delete_albums, ids)

    sent = [u for _m, _url, p, _j in calls for u in p["uris"].split(",")]
    assert sent == [f"spotify:album:{i}" for i in ids]


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


def test_redaction_keeps_the_track_fields_the_like_planner_needs():
    raw = {
        "fetched_at": "2026-01-01T00:00:00Z",
        "albums": [
            {
                "added_at": "2020-01-01T12:34:56Z",
                "album": {
                    "id": "a1",
                    "name": "Record",
                    "album_type": "album",
                    "release_date": "2020-01-01",
                    "release_date_precision": "day",
                    "total_tracks": 2,
                    "artists": [{"id": "ar1", "name": "The Band", "href": "x"}],
                    "images": [],
                    "tracks": {
                        "total": 2,
                        "href": "https://api.spotify.com/secret",
                        "items": [
                            {
                                "id": "t1",
                                "name": "A Song",
                                "duration_ms": 200000,
                                "disc_number": 1,
                                "track_number": 1,
                                "is_local": False,
                                "artists": [{"name": "The Band", "id": "ar1", "uri": "x"}],
                                "available_markets": ["GB"],
                                "uri": "spotify:track:t1",
                            }
                        ],
                    },
                },
            }
        ],
    }
    track = redact_snapshot(raw)["albums"][0]["album"]["tracks"]["items"][0]

    assert track["id"] == "t1"
    assert track["name"] == "A Song"
    assert track["duration_ms"] == 200000
    assert track["disc_number"] == 1 and track["track_number"] == 1
    assert track["is_local"] is False
    assert track["artists"] == [{"name": "The Band"}]


def test_redaction_keeps_the_track_total_so_truncation_is_still_visible():
    raw = {
        "albums": [
            {"added_at": "2020-01-01T00:00:00Z", "album": {"id": "a1", "tracks": {"total": 60, "items": []}}}
        ]
    }
    assert redact_snapshot(raw)["albums"][0]["album"]["tracks"]["total"] == 60


def test_redaction_drops_api_plumbing_from_the_track_listing():
    raw = {
        "albums": [
            {
                "added_at": "2020-01-01T00:00:00Z",
                "album": {
                    "id": "a1",
                    "tracks": {
                        "total": 1,
                        "href": "https://api.spotify.com/secret",
                        "next": "https://api.spotify.com/secret?offset=50",
                        "items": [{"id": "t1", "uri": "spotify:track:t1", "available_markets": ["GB"]}],
                    },
                },
            }
        ]
    }
    tracks = redact_snapshot(raw)["albums"][0]["album"]["tracks"]

    assert set(tracks) == {"total", "items"}
    assert set(tracks["items"][0]) == {
        "id", "name", "duration_ms", "disc_number", "track_number", "is_local", "artists"
    }


def test_redaction_survives_an_album_with_no_track_listing_at_all():
    raw = {"albums": [{"added_at": "2020-01-01T00:00:00Z", "album": {"id": "a1"}}]}
    assert redact_snapshot(raw)["albums"][0]["album"]["tracks"] == {"total": 0, "items": []}


def test_redaction_is_deterministic():
    snapshot = {"fetched_at": "2026-01-01T00:00:00+00:00", "albums": [RAW_ITEM]}
    assert json.dumps(redact_snapshot(snapshot)) == json.dumps(redact_snapshot(snapshot))


def test_redaction_survives_a_malformed_item():
    redacted = redact_snapshot({"albums": [{"album": {"id": "x"}}]})
    album = redacted["albums"][0]["album"]
    assert redacted["albums"][0]["added_at"] is None
    assert album["artists"] == [] and album["images"] == []


# -- the write verbs, on a session that only records what it was asked --------

# The API itself is never mocked; what is checked here is only which verb and path
# each write picks, because getting `remove_tracks` wrong would delete saved albums.


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    def put_ids(self, path: str, ids: list[str]) -> None:
        self.calls.append(("PUT", path, list(ids)))

    def delete_ids(self, path: str, ids: list[str]) -> None:
        self.calls.append(("DELETE", path, list(ids)))


def test_removing_tracks_deletes_against_the_tracks_endpoint():
    session = RecordingSession()
    SpotifyClient(session).remove_tracks(["t1", "t2"])
    assert session.calls == [("DELETE", "/me/tracks", ["t1", "t2"])]


def test_removing_tracks_never_touches_the_albums_endpoint():
    """The one mistake in this method that would be catastrophic and silent."""
    session = RecordingSession()
    SpotifyClient(session).remove_tracks(["t1"])
    assert all(path != "/me/albums" for _verb, path, _ids in session.calls)


def test_removing_more_tracks_than_one_request_allows_is_split():
    session = RecordingSession()
    SpotifyClient(session).remove_tracks([f"t{i}" for i in range(120)])
    assert [len(ids) for _verb, _path, ids in session.calls] == [50, 50, 20]
