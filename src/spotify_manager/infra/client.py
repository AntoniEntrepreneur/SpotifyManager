"""Spotify Web API calls, expressed on top of the shared rate-limited session.

The listing endpoint returns every field the deduplication planner needs -- album id,
artist ids and names, album_type, release_date and its precision, total_tracks, and
cover-art URLs -- so no per-album request is ever made. A full run costs
ceil(library_size / 50) requests, not one per album.

It also embeds each album's first page of tracks, which is why liking every track on
every saved album costs no per-album request either: only an album with more tracks
than that page holds needs `fetch_album_tracks`, and on a real 1281-album library that
is a handful of albums, not 1281.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

#: The maximum page size /v1/me/albums permits.
PAGE_LIMIT = 50
#: The maximum number of items one library write may carry. Spotify's February 2026
#: Web API changes replaced the per-entity writes (PUT/DELETE /v1/me/albums and
#: /v1/me/tracks, which took 50 ids in a JSON body) with a single /v1/me/library
#: endpoint, which takes at most 40 URIs in the query string: 41 is answered with
#: `400 Too many uris requested`.
ID_BATCH_LIMIT = 40


class SpotifyClient:
    def __init__(self, session: Any) -> None:
        self._session = session

    @property
    def session(self) -> Any:
        """The session underneath, so a caller can read its request count."""
        return self._session

    def fetch_all_saved_albums(self) -> tuple[list[dict[str, Any]], int]:
        """Return (every saved-album item, number of pages fetched).

        Pages are requested at the API maximum and followed via the paging object's
        `next` link until it is null, so nothing is lost at a page boundary.
        """
        items: list[dict[str, Any]] = []
        pages = 0
        url: str | None = "/me/albums"
        params: dict[str, Any] | None = {"limit": PAGE_LIMIT, "offset": 0}

        while url:
            page = self._session.get_json(url, params=params)
            pages += 1
            items.extend(page.get("items") or [])
            url = page.get("next")
            params = None  # `next` is a fully-formed URL, already carrying its params

        return items, pages

    def fetch_album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        """Every track on one album.

        Only needed for an album whose embedded track page is truncated -- the saved
        albums listing already carries the rest. Paged the same way as everything
        else, so a 100-track box set is not silently cut in half a second time.
        """
        items: list[dict[str, Any]] = []
        url: str | None = f"/albums/{album_id}/tracks"
        params: dict[str, Any] | None = {"limit": PAGE_LIMIT, "offset": 0}

        while url:
            page = self._session.get_json(url, params=params)
            items.extend(page.get("items") or [])
            url = page.get("next")
            params = None

        return items

    def fetch_all_saved_tracks(self) -> tuple[list[dict[str, Any]], int]:
        """Return (every liked-track item, number of pages fetched).

        The same shape as `fetch_all_saved_albums`, against `/me/tracks`. Only the
        track ids are ever used, but the listing is cached verbatim like the album
        one, so a later feature that needs more does not have to refetch.
        """
        items: list[dict[str, Any]] = []
        pages = 0
        url: str | None = "/me/tracks"
        params: dict[str, Any] | None = {"limit": PAGE_LIMIT, "offset": 0}

        while url:
            page = self._session.get_json(url, params=params)
            pages += 1
            items.extend(page.get("items") or [])
            url = page.get("next")
            params = None

        return items, pages

    # -- library writes ------------------------------------------------------
    #
    # All three go through the one `/v1/me/library` endpoint, which identifies items
    # by URI rather than by bare id, so each takes the ids its callers speak in and
    # converts them on the way out. Callers never see a URI.

    def save_tracks(self, ids: list[str]) -> None:
        for chunk in _chunks(ids, ID_BATCH_LIMIT):
            self._session.put_uris(_uris("track", chunk))

    def remove_tracks(self, ids: list[str]) -> None:
        for chunk in _chunks(ids, ID_BATCH_LIMIT):
            self._session.delete_ids("/me/tracks", chunk)

    def delete_albums(self, ids: list[str]) -> None:
        for chunk in _chunks(ids, ID_BATCH_LIMIT):
            self._session.delete_uris(_uris("album", chunk))

    def save_albums(self, ids: list[str]) -> None:
        for chunk in _chunks(ids, ID_BATCH_LIMIT):
            self._session.put_uris(_uris("album", chunk))


def _uris(kind: str, ids: list[str]) -> list[str]:
    """`spotify:album:xyz` for each id -- the only form library writes accept."""
    return [f"spotify:{kind}:{i}" for i in ids]


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
