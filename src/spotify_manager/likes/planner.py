"""Deciding exactly which track ids to like, and why every other track was not.

Pure: two snapshots in -- the saved albums with their tracks, and the set of track
ids already in Liked Songs -- and a plan out. No network, no filesystem, no clock.
Every rule this feature has lives here, which is what makes them all testable
without touching Spotify.

Four things can be true of a track, and exactly one of them is:

* **unlikeable**   -- a local file, or a track with no id. Nothing can be done with
  it, so it is counted and named rather than attempted.
* **already liked** -- its id is in Liked Songs. Nothing needs to be done.
* **collapsed**    -- it is the same recording as another track that is already liked
  or that this run will like. Liking it too would put the same song in Liked Songs
  twice, from two editions of the same record.
* **to like**      -- everything else.

**On collapsing.** Two saved editions of one album -- a standard and a deluxe, a
release and its remaster -- hold the same recordings under different track ids.
Spotify will happily add both, and on a real 1281-album library that is around 1,237
duplicated songs. Recordings are matched on `(track name, artist names, duration to
the nearest second)`. Exact-millisecond matching would call a remaster a different
recording, which for this purpose it is not; a wider tolerance starts colliding
genuine radio edits.

That is a heuristic, and it is the only one here. The exact answer is the ISRC, which
identifies a recording rather than a release -- but ISRCs are not in the album
listing, so using them would cost a per-50-track fetch across the whole library. The
trade is recorded in an ADR, together with what it costs: a wrong collapse means a
song is quietly never liked, which is the only failure in this feature that does not
announce itself.

**Which copy survives.** The richest edition, using the deduplicator's own ranking --
there is one idea in this codebase of what a good edition is, and it is not
re-invented here -- with one addition: a compilation ranks below every non-
compilation, so a song is taken from the record it belongs to rather than from a
greatest-hits. Ties break on album id and then track id, so the choice is total and
two runs of the same plan produce the same ids.

**Order.** Likes go out in the order the albums were saved, oldest first, and in disc
and track order within an album. Every track this run likes is stamped by Spotify
with the moment of the run, so the block of new likes arrives as one batch whose only
internal ordering is the one chosen here: the library's own history.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..dedupe.models import SavedAlbum
from ..dedupe.ranking import edition_of
from .models import AlbumTracks, Track, TrackLibrary

#: Album type that must never win a collapse against a real album.
COMPILATION = "compilation"


@dataclass(frozen=True)
class LikePlan:
    """What a run intends to do, and what it decided about everything else.

    The counts are not decoration: every track found on every album appears in
    exactly one of them, so the results block can account for all of them and no
    track can go missing without a number moving.
    """

    track_ids: tuple[str, ...] = ()
    albums_scanned: int = 0
    tracks_found: int = 0
    already_liked_count: int = 0
    collapsed_count: int = 0
    unlikeable_count: int = 0
    unlikeable_names: tuple[str, ...] = ()
    incomplete_album_ids: tuple[str, ...] = ()
    liked_songs_known: int = 0

    @property
    def to_like_count(self) -> int:
        return len(self.track_ids)

    @property
    def nothing_to_do(self) -> bool:
        return not self.track_ids

    @property
    def accounted_for(self) -> int:
        """Every track found, classified. Equal to `tracks_found` by construction."""
        return (
            self.to_like_count
            + self.already_liked_count
            + self.collapsed_count
            + self.unlikeable_count
        )


@dataclass(frozen=True)
class _Row:
    """One track, on one album, at one position in the run's chosen order."""

    index: int
    track: Track
    album: SavedAlbum


def recording_key(track: Track) -> tuple[str, frozenset[str], int]:
    """What makes two tracks the same recording, for this feature's purposes.

    Name and artists compared case-insensitively, duration to the nearest second.
    """
    return (
        " ".join(track.name.lower().split()),
        frozenset(name.lower().strip() for name in track.artist_names),
        round(track.duration_ms / 1000),
    )


def keeper_key(row: _Row) -> tuple[int, int, int, tuple[int, int, int], str, str]:
    """Which of two copies of one recording to like. Higher wins.

    The deduplicator's edition ranking, with compilations pushed below everything
    else, then its own tiebreakers, then ids so the ordering is total.
    """
    album = row.album
    _category, rank = edition_of(album)
    not_a_compilation = 0 if album.album_type == COMPILATION else 1
    return (
        not_a_compilation,
        rank,
        album.total_tracks,
        album.release_sort_key,
        album.id,
        row.track.id,
    )


def _album_order_key(entry: AlbumTracks) -> tuple[str, str]:
    """Oldest saved first; album id only to make the order total."""
    return (entry.album.added_at, entry.album.id)


def _track_order_key(track: Track) -> tuple[int, int, str]:
    return (track.disc_number, track.track_number, track.id)


def plan_likes(library: TrackLibrary, liked_ids: frozenset[str]) -> LikePlan:
    """Work out which track ids to like, from the two snapshots and nothing else."""
    rows: list[_Row] = []
    unlikeable: list[str] = []
    incomplete: list[str] = []
    tracks_found = 0

    for entry in sorted(library.albums, key=_album_order_key):
        if entry.truncated:
            incomplete.append(entry.album.id)
        for track in sorted(entry.tracks, key=_track_order_key):
            tracks_found += 1
            if not track.is_likeable:
                unlikeable.append(f"{track.name or '(untitled)'} - {entry.album.name}")
                continue
            rows.append(_Row(index=len(rows), track=track, album=entry.album))

    groups: dict[tuple[str, frozenset[str], int], list[_Row]] = {}
    for row in rows:
        groups.setdefault(recording_key(row.track), []).append(row)

    chosen: list[_Row] = []
    already_liked = 0
    collapsed = 0

    for members in groups.values():
        liked_here = [r for r in members if r.track.id in liked_ids]
        already_liked += len(liked_here)
        candidates = [r for r in members if r.track.id not in liked_ids]
        if not candidates:
            continue
        if liked_here:
            # This recording is already in Liked Songs. Every other copy of it is a
            # duplicate of something the user already has, not something missing.
            collapsed += len(candidates)
            continue
        winner = max(candidates, key=keeper_key)
        chosen.append(winner)
        collapsed += len(candidates) - 1

    chosen.sort(key=lambda row: row.index)
    return LikePlan(
        track_ids=tuple(row.track.id for row in chosen),
        albums_scanned=len(library.albums),
        tracks_found=tracks_found,
        already_liked_count=already_liked,
        collapsed_count=collapsed,
        unlikeable_count=len(unlikeable),
        unlikeable_names=tuple(unlikeable),
        incomplete_album_ids=tuple(incomplete),
        liked_songs_known=len(liked_ids),
    )


def format_plan(plan: LikePlan) -> str:
    """The plan in the terminal: what will be liked, and what will not, and why.

    This is what the confirmation prompt shows, so it has to be readable by someone
    deciding whether to mutate ten thousand rows of their library.
    """
    lines = [
        f"Scanned {plan.albums_scanned} saved albums holding {plan.tracks_found} tracks.",
        f"Already in Liked Songs: {plan.already_liked_count}"
        f" (of {plan.liked_songs_known} liked tracks in total).",
        f"Same recording on another saved edition: {plan.collapsed_count}"
        " (one copy of each will be liked).",
    ]
    if plan.unlikeable_count:
        lines.append(
            f"Cannot be liked: {plan.unlikeable_count} (local files, or no track id)."
        )
        lines += [f"  {name}" for name in plan.unlikeable_names[:20]]
        if plan.unlikeable_count > 20:
            lines.append(f"  ... and {plan.unlikeable_count - 20} more")
    if plan.incomplete_album_ids:
        lines.append(
            f"Track list incomplete for {len(plan.incomplete_album_ids)} album(s); "
            "only the tracks that were listed can be liked:"
        )
        lines += [f"  {album_id}" for album_id in plan.incomplete_album_ids]
    lines.append("")
    if plan.nothing_to_do:
        lines.append("Every track on every saved album is already liked. Nothing to do.")
    else:
        lines.append(f"TO LIKE: {plan.to_like_count} tracks.")
    return "\n".join(lines)
