"""Carrying out a like plan: record first, then the writes, then the truth.

The order is the whole design. `like` writes the run record, flushes it and fsyncs it
to the disk before the first PUT leaves the process; if the record cannot be written,
not one track is liked. Ten thousand unidentifiable likes are far worse than a run
that did not happen.

Everything about issuing the batches -- the size, the retries, the three-way
``succeeded`` / ``failed`` / ``never_attempted`` classification, the rule that a
failed batch does not abandon the following ones and that an interrupt stops the run
at once -- is `spotify_manager.batching`, shared with the deletion path. This module
owns only the part that is this feature's own: the record file, and the vocabulary
the results block reads it by.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..batching import DEFAULT_MAX_ATTEMPTS, BatchResult, run_batches
from ..infra.client import ID_BATCH_LIMIT
from .record import write_record_file

#: Filename stamp for the run record, matching the restore file's, so one run's
#: artefacts sort together whichever directory they are in.
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"


@dataclass(frozen=True)
class LikeResult(BatchResult):
    """What one like run did. `liked_ids` is `succeeded_ids`, named for this feature."""

    record_path: Path | None = None
    created_at: str = ""

    @property
    def liked_ids(self) -> tuple[str, ...]:
        return self.succeeded_ids

    @property
    def liked_count(self) -> int:
        return self.succeeded_count


def like(
    track_ids: tuple[str, ...],
    client: Any,
    *,
    likes_dir: Path,
    now: datetime | None = None,
    batch_size: int = ID_BATCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[str], None] | None = None,
) -> LikeResult:
    """Record the intent, then like the tracks, then say what happened.

    Args:
        track_ids: the ids to like, in the order they should go out. Trusted
            completely -- the planner decided them, as `execute` trusts a
            `Resolution`.
        client: anything with `save_tracks(list[str])`. One call per batch.
        likes_dir: directory the run record is written into.
        now: the instant stamped on the record and its name. Injected so tests do not
            race the clock.
        max_attempts, sleep, on_progress: as in `dedupe.execute.execute`.

    Returns:
        A `LikeResult` in which every requested track id appears in exactly one
        classification.

    Raises:
        RecordFileError: if the run record could not be written. Nothing was liked.
    """
    moment = now or datetime.now().astimezone()
    created_at = moment.isoformat(timespec="seconds")
    stamp = moment.strftime(STAMP_FORMAT)
    requested = tuple(track_ids)

    def say(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    if not requested:
        # Nothing to like is a legitimate outcome, not a special case to skip past.
        # No record file is written, because there is nothing to undo.
        say("Nothing to like. No run record, no requests.")
        return LikeResult(requested_ids=(), created_at=created_at)

    record_path = write_record_file(
        requested, likes_dir, created_at=created_at, stamp=stamp
    )
    say(f"Run record written to {record_path} ({len(requested)} tracks).")

    outcomes, interrupted = run_batches(
        client.save_tracks,
        requested,
        noun="tracks",
        verb="liking",
        batch_size=batch_size,
        max_attempts=max_attempts,
        sleep=sleep,
        say=say,
    )

    result = LikeResult(
        batches=outcomes,
        requested_ids=requested,
        interrupted=interrupted,
        record_path=record_path,
        created_at=created_at,
    )
    say(
        f"Done: {result.liked_count} liked, {result.failed_count} failed, "
        f"{result.never_attempted_count} never attempted."
    )
    return result
