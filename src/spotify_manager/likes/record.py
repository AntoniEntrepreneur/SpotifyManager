"""The run record: the only thing that can undo a run that liked ten thousand tracks.

Liking is reversible -- `DELETE /v1/me/tracks` exists -- but only for someone who
knows *which* tracks to remove. Nothing in Spotify distinguishes a track this tool
liked from one the user liked by hand three years ago, so if the run does not write
its own list down, an unwanted run cannot be undone at all.

Two rules, both borrowed from the restore file `dedupe` writes for the same reason:

**It is written before the first request, not after.** Fsynced, not merely handed to
the OS -- the moment we know a like worked is the moment it is too late to record it.
If the record cannot be written, nothing is liked.

**It records what the run set out to do, not what it managed to do.** Removing a like
that was never added is a no-op on Spotify's side, so a record that is a superset of
what happened is harmless; one that is a subset leaves likes the user cannot find.

What is deliberately *not* in the file is the ids the planner collapsed away as
duplicates. This file exists to be handed to a delete, and must contain nothing that
was not sent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SpotifyManagerError

#: Bumped only if the shape below changes in a way an older reader would misread.
RECORD_VERSION = 1


class RecordFileError(SpotifyManagerError):
    """The run record could not be written, so nothing may be liked."""


class RecordDocumentError(SpotifyManagerError):
    """The file is not a run record this tool can act on."""


@dataclass(frozen=True)
class LikeRecord:
    """A parsed run record: the track ids one run set out to like."""

    track_ids: tuple[str, ...]
    created_at: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> LikeRecord:
        """Validate a decoded run record completely, or refuse it.

        Only `track_ids` is required; `created_at` is for a human reading the file.

        Raises:
            RecordDocumentError: if `raw` is not a JSON object or its `track_ids` is
                not a list of strings.
        """
        if not isinstance(raw, dict):
            raise RecordDocumentError(
                "A run record must be a JSON object with a 'track_ids' field."
            )
        ids = raw.get("track_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise RecordDocumentError(
                "A run record must have a 'track_ids' field listing track ids as "
                "strings."
            )
        return cls(track_ids=tuple(ids), created_at=str(raw.get("created_at") or ""))


def document_for(track_ids: tuple[str, ...], *, created_at: str) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "created_at": created_at,
        "track_ids": list(track_ids),
    }


def write_record_file(
    track_ids: tuple[str, ...],
    directory: Path,
    *,
    created_at: str,
    stamp: str,
) -> Path:
    """Write the run record and make sure it is really on the disk.

    Raises:
        RecordFileError: if anything at all goes wrong. The caller must treat this as
            fatal and like nothing.
    """
    path = directory / f"liked-{stamp}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document_for(track_ids, created_at=created_at), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RecordFileError(
            f"Could not write the run record to {path}: {exc}\n"
            "Nothing was liked: this tool does not add likes it cannot tell you how "
            "to remove again."
        ) from exc
    return path
