"""The data the planner consumes and produces.

The snapshot on disk holds `/me/albums` items exactly as Spotify returned them, so
the mapping from raw dictionaries to these types lives here -- it is the one place
that knows the API's field names, and it keeps the rest of the package working with
plain, typed values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# An unordered pair of album ids the user has asserted are *not* duplicates.
AssertedDistinct = frozenset[frozenset[str]]


@dataclass(frozen=True)
class Artist:
    id: str
    name: str


@dataclass(frozen=True)
class Image:
    url: str
    height: int | None = None
    width: int | None = None


@dataclass(frozen=True)
class SavedAlbum:
    id: str
    name: str
    artists: tuple[Artist, ...]
    album_type: str
    release_date: str
    release_date_precision: str
    total_tracks: int
    images: tuple[Image, ...] = ()
    added_at: str = ""

    @property
    def primary_artist(self) -> Artist | None:
        return self.artists[0] if self.artists else None

    @property
    def primary_artist_id(self) -> str:
        artist = self.primary_artist
        return artist.id if artist else ""

    @property
    def artist_names(self) -> str:
        return ", ".join(a.name for a in self.artists)

    @property
    def release_sort_key(self) -> tuple[int, int, int]:
        """A comparable date, padding a partial release date to its earliest day.

        Spotify returns year-, month- or day-precision dates. Padding missing parts
        with 1 means a year-precision 2019 sorts before 2019-09-27, which is the
        honest reading: we only know it was somewhere in 2019.
        """
        parts = (self.release_date or "").split("-")
        numbers = []
        for index in range(3):
            try:
                numbers.append(int(parts[index]))
            except (IndexError, ValueError):
                numbers.append(1 if index else 0)
        return (numbers[0], numbers[1], numbers[2])


@dataclass(frozen=True)
class LibrarySnapshot:
    fetched_at: str
    albums: tuple[SavedAlbum, ...]


@dataclass(frozen=True)
class GroupKey:
    normalized_title: str
    primary_artist_id: str
    album_type: str


@dataclass(frozen=True)
class AlbumJudgement:
    album: SavedAlbum
    edition_rank: int
    edition_category: str
    ignored_decorations: tuple[str, ...]
    is_keeper: bool


@dataclass(frozen=True)
class DuplicateGroup:
    key: GroupKey
    members: tuple[AlbumJudgement, ...]
    keeper_id: str
    ignored_decorations: tuple[str, ...]
    suppressed_pair_count: int = 0

    @property
    def keeper(self) -> AlbumJudgement:
        return next(m for m in self.members if m.album.id == self.keeper_id)

    @property
    def removals(self) -> tuple[AlbumJudgement, ...]:
        return tuple(m for m in self.members if m.album.id != self.keeper_id)


@dataclass(frozen=True)
class DedupePlan:
    groups: tuple[DuplicateGroup, ...] = ()
    total_albums_scanned: int = 0
    excluded_reasons: dict[str, int] = field(default_factory=dict)
    suppressed_group_count: int = 0

    @property
    def excluded_count(self) -> int:
        return sum(self.excluded_reasons.values())

    @property
    def albums_in_groups(self) -> int:
        return sum(len(g.members) for g in self.groups)

    @property
    def proposed_removal_count(self) -> int:
        return sum(len(g.members) - 1 for g in self.groups)

    @property
    def proposed_removal_ids(self) -> tuple[str, ...]:
        return tuple(m.album.id for g in self.groups for m in g.removals)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def album_from_raw(raw: dict[str, Any], added_at: str = "") -> SavedAlbum:
    """Map one album object from the API's saved-albums listing."""
    artists = tuple(
        Artist(id=str(a.get("id") or ""), name=str(a.get("name") or ""))
        for a in raw.get("artists") or []
    )
    images = tuple(
        Image(
            url=str(i.get("url") or ""),
            height=_as_int(i.get("height"), 0) or None,
            width=_as_int(i.get("width"), 0) or None,
        )
        for i in raw.get("images") or []
    )
    return SavedAlbum(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        artists=artists,
        album_type=str(raw.get("album_type") or "").lower(),
        release_date=str(raw.get("release_date") or ""),
        release_date_precision=str(raw.get("release_date_precision") or ""),
        total_tracks=_as_int(raw.get("total_tracks"), 0),
        images=images,
        added_at=added_at,
    )


def snapshot_from_raw(raw: dict[str, Any]) -> LibrarySnapshot:
    """Map a cached snapshot document (`{fetched_at, albums: [saved-album item]}`).

    Items without an album id are dropped: an album we cannot address is one we
    could never act on anyway.
    """
    albums: list[SavedAlbum] = []
    for item in raw.get("albums") or []:
        album_raw = item.get("album") if isinstance(item, dict) else None
        if not isinstance(album_raw, dict):
            continue
        album = album_from_raw(album_raw, added_at=str(item.get("added_at") or ""))
        if album.id:
            albums.append(album)
    return LibrarySnapshot(fetched_at=str(raw.get("fetched_at") or ""), albums=tuple(albums))
