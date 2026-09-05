"""The data the like planner consumes, and the mapping from the snapshots on disk.

The saved-albums listing embeds each album's first page of tracks, so the snapshot
this tool already keeps holds almost everything this feature needs. What that means
in practice is that the album type here is the *same* `SavedAlbum` the deduplicator
works with -- there is one idea of a saved album in this codebase, and the edition
ranking that decides which copy of a duplicate recording to keep can only be reused
if it is being handed the same value.

`AlbumTracks` is that album plus its tracks. `truncated` records the one thing the
listing cannot tell us on its own: whether the embedded page was the whole album. A
truncated album is completed by the I/O shell before planning, or reported as
incomplete -- the planner itself never fetches anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..dedupe.models import SavedAlbum, album_from_raw


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artist_names: tuple[str, ...]
    duration_ms: int
    disc_number: int = 1
    track_number: int = 1
    is_local: bool = False

    @property
    def is_likeable(self) -> bool:
        """Whether this track can be saved at all.

        A local file has no place in anyone's Spotify library and a track with no id
        cannot be addressed. Neither is an error; both are things to report rather
        than to attempt.
        """
        return bool(self.id) and not self.is_local


@dataclass(frozen=True)
class AlbumTracks:
    album: SavedAlbum
    tracks: tuple[Track, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class TrackLibrary:
    fetched_at: str
    albums: tuple[AlbumTracks, ...] = ()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def track_from_raw(raw: dict[str, Any]) -> Track:
    """Map one simplified track object, as the album listing returns it."""
    return Track(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        artist_names=tuple(
            str(a.get("name") or "") for a in raw.get("artists") or [] if isinstance(a, dict)
        ),
        duration_ms=_as_int(raw.get("duration_ms"), 0),
        disc_number=_as_int(raw.get("disc_number"), 1),
        track_number=_as_int(raw.get("track_number"), 1),
        is_local=bool(raw.get("is_local")),
    )


def album_tracks_from_raw(item: dict[str, Any]) -> AlbumTracks | None:
    """Map one saved-album item into an album and the tracks it carries.

    Returns None for an item with no addressable album, exactly as the dedupe
    snapshot mapping drops it: an album we cannot address is one we could never act
    on anyway.
    """
    raw_album = item.get("album") if isinstance(item, dict) else None
    if not isinstance(raw_album, dict):
        return None
    album = album_from_raw(raw_album, added_at=str(item.get("added_at") or ""))
    if not album.id:
        return None

    paging = raw_album.get("tracks")
    paging = paging if isinstance(paging, dict) else {}
    raw_tracks = [t for t in paging.get("items") or [] if isinstance(t, dict)]
    tracks = tuple(track_from_raw(t) for t in raw_tracks)
    total = _as_int(paging.get("total"), len(tracks))
    return AlbumTracks(album=album, tracks=tracks, truncated=total > len(tracks))


def library_from_raw(raw: dict[str, Any]) -> TrackLibrary:
    """Map a cached saved-albums snapshot into albums with their tracks."""
    albums = []
    for item in raw.get("albums") or []:
        mapped = album_tracks_from_raw(item) if isinstance(item, dict) else None
        if mapped is not None:
            albums.append(mapped)
    return TrackLibrary(fetched_at=str(raw.get("fetched_at") or ""), albums=tuple(albums))


def liked_ids_from_raw(raw: dict[str, Any]) -> frozenset[str]:
    """The set of track ids in a cached Liked Songs snapshot.

    Only the ids are read. The snapshot stores the listing verbatim anyway, so a
    later feature that wants more than an id does not have to refetch to get it.
    """
    ids = set()
    for item in raw.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        track = item.get("track")
        if not isinstance(track, dict):
            continue
        track_id = str(track.get("id") or "")
        if track_id:
            ids.add(track_id)
    return frozenset(ids)
