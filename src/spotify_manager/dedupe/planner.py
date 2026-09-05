"""From a library snapshot to a dedupe plan.

`plan` is a pure function: same snapshot in, same plan out, with no network, no
files and no clock anywhere beneath it. Every matching and ranking judgement the
tool makes passes through here, which is why this is the seam the tests drive.

Three conditions must all hold before two albums are considered the same record:
their normalized titles are equal, their primary artist ids are equal, and their
release types are equal. Compilations never take part at all.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .models import (
    AlbumJudgement,
    AssertedDistinct,
    DedupePlan,
    DuplicateGroup,
    GroupKey,
    LibrarySnapshot,
    SavedAlbum,
)
from .normalize import normalize_title
from .ranking import best_category, keeper_sort_key, rank_of

EXCLUDED_COMPILATION = "compilation"


def plan(
    snapshot: LibrarySnapshot,
    asserted_distinct: AssertedDistinct = frozenset(),
) -> DedupePlan:
    """Work out which saved albums are duplicate editions of each other.

    `asserted_distinct` holds unordered pairs of album ids the user has already
    judged to be different records. A group in which *every* pairwise comparison is
    already judged has nothing left to ask about and is dropped from the plan; a
    group with any unjudged comparison is shown whole, so the remaining question is
    asked in its full context.
    """
    excluded_reasons: dict[str, int] = {}
    buckets: dict[GroupKey, list[SavedAlbum]] = defaultdict(list)

    for album in snapshot.albums:
        if album.album_type == EXCLUDED_COMPILATION:
            excluded_reasons[EXCLUDED_COMPILATION] = (
                excluded_reasons.get(EXCLUDED_COMPILATION, 0) + 1
            )
            continue
        key = GroupKey(
            normalized_title=normalize_title(album.name).title,
            primary_artist_id=album.primary_artist_id,
            album_type=album.album_type,
        )
        buckets[key].append(album)

    groups: list[DuplicateGroup] = []
    suppressed_group_count = 0

    for key, members in buckets.items():
        if len(members) < 2:
            continue
        pairs = list(combinations(sorted(a.id for a in members), 2))
        suppressed = sum(1 for pair in pairs if frozenset(pair) in asserted_distinct)
        if suppressed == len(pairs):
            suppressed_group_count += 1
            continue
        groups.append(_build_group(key, members, suppressed))

    groups.sort(key=_group_sort_key)
    return DedupePlan(
        groups=tuple(groups),
        total_albums_scanned=len(snapshot.albums),
        excluded_reasons=excluded_reasons,
        suppressed_group_count=suppressed_group_count,
    )


def _build_group(
    key: GroupKey, members: list[SavedAlbum], suppressed_pair_count: int
) -> DuplicateGroup:
    ranked: list[tuple[SavedAlbum, str, int, tuple[str, ...]]] = []
    for album in members:
        normalized = normalize_title(album.name)
        category = best_category(normalized.categories)
        ranked.append(
            (
                album,
                category,
                rank_of(category),
                tuple(d.text for d in normalized.decorations),
            )
        )

    # Break exact ties on album id, the same tiebreak the sort below uses, so the
    # keeper always agrees with the top (first) row of the sorted member list.
    def _keeper_key(entry: tuple[SavedAlbum, str, int, tuple[str, ...]]) -> tuple:
        return (keeper_sort_key(entry[0], entry[2]), entry[0].id)

    keeper = max(ranked, key=_keeper_key)[0]
    ranked.sort(key=_keeper_key, reverse=True)

    ignored: list[str] = []
    for _, _, _, decorations in ranked:
        for decoration in decorations:
            if decoration not in ignored:
                ignored.append(decoration)

    return DuplicateGroup(
        key=key,
        members=tuple(
            AlbumJudgement(
                album=album,
                edition_rank=rank,
                edition_category=category,
                ignored_decorations=decorations,
                is_keeper=album.id == keeper.id,
            )
            for album, category, rank, decorations in ranked
        ),
        keeper_id=keeper.id,
        ignored_decorations=tuple(ignored),
        suppressed_pair_count=suppressed_pair_count,
    )


def _group_sort_key(group: DuplicateGroup) -> tuple[int, str, str]:
    """Biggest groups first, then alphabetical -- a stable, reviewable order."""
    return (-len(group.members), group.keeper.album.artist_names.lower(), group.key.normalized_title)
