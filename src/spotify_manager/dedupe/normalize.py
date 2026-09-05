"""Allowlist-only title normalization.

The single rule that governs this module: a decoration is removed only when it is
positively recognised as an edition marker. Everything else stays in the normalized
title and therefore keeps two albums apart. An unfamiliar suffix must produce a
missed duplicate, never a wrong deletion.

Decorations are looked for only at the *end* of a title -- a leading or embedded
parenthetical, as in "(What's The Story) Morning Glory?", is part of the name. A
trailing segment is stripped only when the whole of it is recognised: "Deluxe
Edition" goes, "International Deluxe Explicit" stays, because "international" and
"explicit" are not markers and dropping them would be a guess.

This module names edition *categories* only. Turning a category into a number is
`ranking.py`'s job, and nothing else's.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SUPER_DELUXE = "super_deluxe"
DELUXE = "deluxe"
EXPANDED = "expanded"
COMPLETE = "complete"
ANNIVERSARY = "anniversary"
REMASTERED = "remastered"
SPECIAL = "special"
BONUS_TRACK = "bonus_track"
PLAIN = "plain"

# Words allowed to pad a recognised marker without meaning anything themselves.
# A segment made only of these is *not* recognised: at least one real marker is
# required before anything is stripped.
_FILLER = frozenset({"edition", "editions", "version", "the", "and", "a"})

_FEATURE_WORDS = frozenset({"feat", "ft", "featuring"})

_YEAR = re.compile(r"^(19|20)\d{2}$")
_ORDINAL = re.compile(r"^\d{1,3}(st|nd|rd|th)?$")
_DASH_SPLIT = re.compile(r"\s+[-‒–—―]\s+")
_TRAILING_FEATURE = re.compile(
    r"\s+(feat|ft|featuring)\.?\s+\S.*$", re.IGNORECASE
)


@dataclass(frozen=True)
class Decoration:
    """One decoration that was recognised and ignored, kept verbatim for the audit."""

    text: str
    categories: frozenset[str] = frozenset()  # empty: rank-neutral (year, featured)


@dataclass(frozen=True)
class NormalizedTitle:
    title: str
    decorations: tuple[Decoration, ...]
    categories: frozenset[str]


def normalize_title(name: str) -> NormalizedTitle:
    """Fold a title to its comparable form and report what was ignored to get there."""
    remainder = name.strip()
    decorations: list[Decoration] = []
    categories: set[str] = set()

    while True:
        split = _pop_trailing_segment(remainder)
        if split is None:
            break
        head, segment = split
        matched = _classify_segment(segment)
        if matched is None:
            # Unrecognised: it stays, and so does everything before it.
            break
        decorations.append(Decoration(text=segment.strip(), categories=frozenset(matched)))
        categories.update(matched)
        remainder = head

    # A recognised marker also appears without any bracketing, e.g.
    # "Song feat. Someone". Only the featured-artist form is safe to take bare.
    bare_feature = _TRAILING_FEATURE.search(remainder)
    if bare_feature and _fold(remainder[: bare_feature.start()]).strip():
        decorations.append(Decoration(text=bare_feature.group(0).strip()))
        remainder = remainder[: bare_feature.start()]

    title = _fold(remainder)

    if not title:
        # Stripping left nothing to compare (a title like "?"). Fall back to the
        # whole folded name rather than let every such album share an empty key.
        title = _fold(name)
        decorations = []
        categories = set()

    return NormalizedTitle(
        title=title,
        decorations=tuple(_dedupe_preserving_order(decorations)),
        categories=frozenset(categories),
    )


def _dedupe_preserving_order(decorations: list[Decoration]) -> list[Decoration]:
    seen: set[str] = set()
    out: list[Decoration] = []
    for decoration in reversed(decorations):  # restore left-to-right title order
        if decoration.text.lower() in seen:
            continue
        seen.add(decoration.text.lower())
        out.append(decoration)
    return out


def _pop_trailing_segment(text: str) -> tuple[str, str] | None:
    """Split off the last bracketed or dash-suffixed segment, if there is one."""
    stripped = text.rstrip()
    for opener, closer in (("(", ")"), ("[", "]")):
        if stripped.endswith(closer):
            depth = 0
            for index in range(len(stripped) - 1, -1, -1):
                char = stripped[index]
                if char == closer:
                    depth += 1
                elif char == opener:
                    depth -= 1
                    if depth == 0:
                        head = stripped[:index].rstrip()
                        if not head:
                            return None  # the whole title is bracketed; leave it alone
                        return head, stripped[index + 1 : len(stripped) - 1]
            return None

    matches = list(_DASH_SPLIT.finditer(stripped))
    if matches:
        last = matches[-1]
        head = stripped[: last.start()].rstrip()
        tail = stripped[last.end() :]
        if head and tail:
            return head, tail
    return None


def _classify_segment(segment: str) -> set[str] | None:
    """Return the edition categories a segment contributes, or None if unrecognised.

    A returned empty set means "recognised but rank-neutral" -- a bare year or a
    featured-artist clause.
    """
    tokens = _fold(segment).split()
    if not tokens:
        return None

    if tokens[0] in _FEATURE_WORDS:
        return set()

    categories: set[str] = set()
    matched_marker = False
    index = 0
    while index < len(tokens):
        consumed, category = _match_marker(tokens, index)
        if consumed:
            matched_marker = True
            if category is not None:
                categories.add(category)
            index += consumed
            continue
        if tokens[index] in _FILLER:
            index += 1
            continue
        return None  # an unrecognised word anywhere in the segment keeps all of it

    return categories if matched_marker else None


def _match_marker(tokens: list[str], index: int) -> tuple[int, str | None]:
    """How many tokens a recognised marker consumes at `index`, and its category."""
    token = tokens[index]
    following = tokens[index + 1] if index + 1 < len(tokens) else ""

    if token == "super" and following == "deluxe":
        return 2, SUPER_DELUXE
    if token == "deluxe":
        return 1, DELUXE
    if token == "expanded":
        return 1, EXPANDED
    if token == "complete":
        return 1, COMPLETE
    if token == "anniversary":
        return 1, ANNIVERSARY
    if token in ("remaster", "remastered"):
        return 1, REMASTERED
    if token == "special":
        return 1, SPECIAL
    if token == "bonus" and following in ("track", "tracks"):
        return 2, BONUS_TRACK

    if _YEAR.match(token):
        # A year is part of the marker it introduces ("2011 Remaster"), otherwise a
        # bare year on its own.
        if following in ("remaster", "remastered"):
            return 2, REMASTERED
        return 1, None

    if _ORDINAL.match(token):
        # "10th Anniversary", "10 Year Anniversary"
        rest = tokens[index + 1 :]
        if rest[:1] == ["anniversary"]:
            return 2, ANNIVERSARY
        if rest[:2] == ["year", "anniversary"] or rest[:2] == ["years", "anniversary"]:
            return 3, ANNIVERSARY
        return 0, None

    return 0, None


def _fold(text: str) -> str:
    """Lowercase, drop diacritics and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = without_marks.lower()
    cleaned = "".join(c if c.isalnum() else " " for c in lowered)
    return " ".join(cleaned.split())
