"""The not-duplicates ledger: what the user has already judged *not* the same record.

Skipping a group in the report is a judgement, and it is a judgement about *pairs*
of albums, not about a title, a key or a group. That granularity is the whole point:
having said that A and B are different records, the user is never asked about A-vs-B
again -- but saving C later, which matches the same key, still surfaces A-vs-C and
B-vs-C, because those comparisons have never been judged. A group-level or key-level
record would either re-ask a settled question or silently hide a new one.

This module owns the file. It is the one impure thing in `dedupe/`: it reads and
writes JSON in the state directory. `planner.plan` never calls it -- it takes the
pairs as a plain frozenset value, so the planner stays a pure function of its
arguments and every suppression case is testable without a filesystem.

On disk::

    {
      "version": 1,
      "pairs": [["albumIdA", "albumIdB"], ...]
    }

Each pair is written sorted, and the list of pairs is written sorted, so an unordered
pair has exactly one representation: (a, b) and (b, a) are the same fact and can
never both be stored. Nothing here ever raises on a damaged or missing file -- a
ledger that cannot be read degrades to "no decisions recorded" plus a warning, since
losing past judgements is an annoyance while a traceback in the middle of a run is a
failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import AssertedDistinct

#: File name of the ledger inside the state directory.
LEDGER_FILENAME = "not_duplicates.json"

#: Bumped only if the on-disk shape ever changes. An unknown version is treated the
#: same way as a corrupt file: warn, and proceed as though nothing were recorded.
LEDGER_VERSION = 1


@dataclass(frozen=True)
class LoadedLedger:
    """What was found on disk, and anything the user should be told about it."""

    pairs: AssertedDistinct = frozenset()
    warning: str | None = None

    def __len__(self) -> int:
        return len(self.pairs)


def ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_FILENAME


def load(path: Path) -> LoadedLedger:
    """Read the ledger, degrading to "no decisions recorded" on any problem.

    Never raises. A missing file is the ordinary first-run case and carries no
    warning; anything else that stops the file being understood carries one naming
    the file and what was wrong with it.
    """
    if not path.exists():
        return LoadedLedger()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return LoadedLedger(warning=_unreadable(path, str(exc)))

    try:
        pairs = _pairs_from_raw(raw)
    except ValueError as exc:
        return LoadedLedger(warning=_unreadable(path, str(exc)))
    return LoadedLedger(pairs=pairs)


def record(path: Path, pairs: AssertedDistinct | tuple[frozenset[str], ...]) -> int:
    """Merge `pairs` into the ledger on disk and return how many were new.

    Merge rather than replace: the file accumulates every judgement ever made, and a
    run that skips one group must not discard what earlier runs recorded. Re-writing
    an existing pair is a no-op, which is what makes an unordered pair storable only
    once.
    """
    incoming = frozenset(_valid(pair) for pair in pairs)
    existing = load(path).pairs
    merged = existing | incoming
    added = len(merged) - len(existing)
    if added:
        _write(path, merged)
    return added


def clear(path: Path) -> int:
    """Forget every recorded judgement. Returns how many pairs were discarded."""
    discarded = len(load(path).pairs)
    _write(path, frozenset())
    return discarded


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _write(path: Path, pairs: AssertedDistinct) -> None:
    document = {
        "version": LEDGER_VERSION,
        "pairs": [sorted(pair) for pair in sorted(pairs, key=sorted)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _pairs_from_raw(raw: object) -> AssertedDistinct:
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object at the top level")
    version = raw.get("version")
    if version != LEDGER_VERSION:
        raise ValueError(f"unknown ledger version {version!r}")
    entries = raw.get("pairs")
    if not isinstance(entries, list):
        raise ValueError("'pairs' is missing or is not a list")

    pairs: set[frozenset[str]] = set()
    for entry in entries:
        if not isinstance(entry, list) or not all(isinstance(i, str) for i in entry):
            raise ValueError(f"{entry!r} is not a pair of album ids")
        pairs.add(_valid(frozenset(entry)))
    return frozenset(pairs)


def _valid(pair: frozenset[str]) -> frozenset[str]:
    """A pair is two distinct album ids. Anything else is not a comparison."""
    pair = frozenset(pair)
    if len(pair) != 2:
        raise ValueError(f"{sorted(pair)!r} is not a pair of two distinct album ids")
    return pair


def _unreadable(path: Path, detail: str) -> str:
    return (
        f"Could not read the not-duplicates ledger at {path}: {detail}.\n"
        "Continuing as though no decisions had been recorded, so groups you "
        "previously skipped will be proposed again. Skipping them again will "
        "rewrite the file."
    )
