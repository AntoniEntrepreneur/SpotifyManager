"""How rich an edition is, as one table.

`EDITION_RANK` is the only place in the codebase that knows what an edition is
worth. Retuning the tool's taste is an edit to this dict and nothing else -- no
logic elsewhere may hardcode a rank number, and no other module may map a category
to an integer.
"""

from __future__ import annotations

from .models import SavedAlbum
from .normalize import PLAIN, normalize_title

EDITION_RANK: dict[str, int] = {
    "super_deluxe": 100,
    "complete": 100,
    "anniversary": 100,
    "deluxe": 80,
    "expanded": 80,
    "remastered": 60,
    "special": 40,
    "bonus_track": 40,
    "plain": 0,
}


def rank_of(category: str) -> int:
    """The rank of one edition category; an unknown category ranks as plain."""
    return EDITION_RANK.get(category, EDITION_RANK[PLAIN])


def best_category(categories: frozenset[str] | set[str]) -> str:
    """The richest category among those a title matched.

    A title occasionally carries two markers ("Deluxe ... Remastered"); the richer
    one decides, since the release is at least that rich.
    """
    if not categories:
        return PLAIN
    return max(sorted(categories), key=rank_of)


def edition_of(album: SavedAlbum) -> tuple[str, int]:
    """The album's edition category and its rank."""
    category = best_category(normalize_title(album.name).categories)
    return category, rank_of(category)


def keeper_sort_key(album: SavedAlbum, rank: int) -> tuple[int, int, tuple[int, int, int]]:
    """Richest edition wins; ties break on more tracks, then on a later release."""
    return (rank, album.total_tracks, album.release_sort_key)
