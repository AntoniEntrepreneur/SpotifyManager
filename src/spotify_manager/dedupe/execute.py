"""Carrying out an approved resolution: recovery first, then removal, then the truth.

This is the only module in the package that destroys anything, and its whole shape
follows from that.

**Recovery is written before removal.** `execute` writes the restore file, flushes it
and fsyncs it to the disk before the first DELETE leaves the process. Not after, not
concurrently, and not "as soon as we know it worked" -- the moment we know it worked
is the moment it is too late to write it. If the restore file cannot be written, no
album is removed at all: the run fails with the library untouched, which is the
correct trade because a missing duplicate costs nothing and a missing album is
forever.

Note that the restore file records the albums this run *set out* to remove, not the
albums it managed to remove. That is deliberate. Re-saving an album that is still
saved is a no-op on Spotify's side, so a restore file that is a superset of what was
actually deleted is harmless; one that is a subset is a lost album.

**How the batches are issued, retried and classified is not here.** That problem is
identical for every write this tool makes, and lives in `spotify_manager.batching`:
the three-way ``succeeded`` / ``failed`` / ``never_attempted`` classification, the
rule that a failed batch does not abandon the following ones, and the rule that an
interrupt stops the run at once and is never retried. What stays here is the part
that is dedupe's alone -- that a restore file exists on disk before the first DELETE.

`restore` is the mirror image of `execute`, undoing exactly what a restore file
promises: it shares the batching, the retry-with-backoff and the three-way
classification, because a restore run can fail in every way a deletion run can and
deserves the same honesty about it. It has no restore file of its own to write --
the file it is restoring *from* is already that record -- and it does not need any
special handling for an album that turns out to already be saved, because PUT
`/v1/me/library` is idempotent on Spotify's side: re-saving a saved album is a
no-op, not an error, so it is simply reported as succeeded like any other.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..batching import (
    DEFAULT_MAX_ATTEMPTS,
    FAILED,
    NEVER_ATTEMPTED,
    SUCCEEDED,
    BatchOutcome,
    BatchResult,
    batches_of,
    run_batches,
)
from ..errors import SpotifyManagerError
from ..infra.client import ID_BATCH_LIMIT
from .resolve import Resolution

__all__ = [
    "Applied",
    "BatchOutcome",
    "DEFAULT_MAX_ATTEMPTS",
    "ExecutionResult",
    "FAILED",
    "NEVER_ATTEMPTED",
    "RestoreFileError",
    "SUCCEEDED",
    "batches_of",
    "execute",
    "restore",
    "restore_command_for",
    "write_restore_file",
]

#: Filename stamp shared by the restore file and the archived results report, so the
#: two halves of one run's record sort together and are obviously the same run.
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"

#: What the results view tells the user to type to undo this run.
RESTORE_COMMAND = "spotify-manager restore"


class RestoreFileError(SpotifyManagerError):
    """The restore file could not be written, so nothing may be deleted."""


@dataclass(frozen=True)
class ExecutionResult(BatchResult):
    """What one deletion or restoration run did, batch by batch.

    The classification lives in `BatchResult`; what this adds is the pair of facts
    only a dedupe run has -- where the restore file went, and when the run happened.
    `removed_ids` is `succeeded_ids` under the name this feature reads it by.
    """

    restore_path: Path | None = None
    created_at: str = ""

    @property
    def removed_ids(self) -> tuple[str, ...]:
        return self.succeeded_ids

    @property
    def removed_count(self) -> int:
        return self.succeeded_count


@dataclass(frozen=True)
class Applied:
    """A decision that has been carried out: what was approved, and what happened.

    The pair travels together because neither half is readable alone -- the result
    holds ids, and the resolution holds the album names those ids belong to.
    """

    resolution: Resolution
    result: ExecutionResult
    recorded_pairs: int = 0


def write_restore_file(
    resolution: Resolution,
    directory: Path,
    *,
    created_at: str,
    stamp: str,
) -> Path:
    """Write the restore file and make sure it is really on the disk.

    Flushed and fsynced before returning, because "written" here has to mean survives
    the process being killed one instruction later -- an OS buffer holding the only
    record of what we are about to delete is not a record.

    Raises:
        RestoreFileError: if anything at all goes wrong. The caller must treat this
            as fatal and delete nothing.
    """
    path = directory / f"restore-{stamp}.json"
    document = resolution.restore_document(created_at=created_at)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RestoreFileError(
            f"Could not write the restore file to {path}: {exc}\n"
            "Nothing was deleted: this tool does not remove an album it cannot "
            "tell you how to get back."
        ) from exc
    return path


def execute(
    resolution: Resolution,
    client: Any,
    *,
    restores_dir: Path,
    now: datetime | None = None,
    batch_size: int = ID_BATCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[str], None] | None = None,
) -> ExecutionResult:
    """Write the restore file, then remove the resolved albums, then say what happened.

    Args:
        resolution: what the reviewer approved. Only `to_delete` and the restore
            payload are used.
        client: anything with `delete_albums(list[str])`. One call per batch, so the
            batching -- and therefore the classification -- is decided here.
        restores_dir: directory the restore file is written into.
        now: the instant stamped on the restore file and its name. Injected so tests
            do not race the clock.
        max_attempts: total issues of one batch before it is called failed.
        sleep: injected so the backoff schedule can be asserted without waiting.
        on_progress: called with one line per notable step, for the terminal.

    Returns:
        An `ExecutionResult` in which every requested album id appears in exactly one
        classification.

    Raises:
        RestoreFileError: if the restore file could not be written. Nothing was
            deleted in that case.
    """
    moment = now or datetime.now().astimezone()
    created_at = moment.isoformat(timespec="seconds")
    stamp = moment.strftime(STAMP_FORMAT)
    requested = tuple(resolution.to_delete)

    def say(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    if not requested:
        # Approving nothing is a legitimate outcome, not a special case to skip past:
        # it still produces a result, and a results view, saying exactly that. No
        # restore file is written, because there is nothing to restore.
        say("Nothing was approved for removal. No restore file, no requests.")
        return ExecutionResult(requested_ids=(), created_at=created_at)

    restore_path = write_restore_file(
        resolution, restores_dir, created_at=created_at, stamp=stamp
    )
    say(f"Restore file written to {restore_path} ({len(requested)} albums).")

    outcomes, interrupted = run_batches(
        client.delete_albums,
        requested,
        noun="albums",
        verb="removing",
        batch_size=batch_size,
        max_attempts=max_attempts,
        sleep=sleep,
        say=say,
    )

    result = ExecutionResult(
        batches=outcomes,
        requested_ids=requested,
        restore_path=restore_path,
        created_at=created_at,
        interrupted=interrupted,
    )
    say(
        f"Done: {result.removed_count} removed, {result.failed_count} failed, "
        f"{result.never_attempted_count} never attempted."
    )
    return result


def restore(
    album_ids: tuple[str, ...],
    client: Any,
    *,
    batch_size: int = ID_BATCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[str], None] | None = None,
) -> ExecutionResult:
    """Re-save every album a restore file listed, then say what happened.

    Args:
        album_ids: the ids to re-save, already read and validated by the caller
            (`dedupe.restore.RestoreDocument.from_raw`) -- this function trusts its
            input completely, the same way `execute` trusts a `Resolution`.
        client: anything with `save_albums(list[str])`. One call per batch, so the
            batching -- and therefore the classification -- is decided here.
        max_attempts, sleep, on_progress: as in `execute`.

    Returns:
        An `ExecutionResult` in which every id appears in exactly one classification.
        `restore_path` is always None: there is nothing to write, only something to
        undo.
    """
    requested = tuple(album_ids)

    def say(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    if not requested:
        say("Nothing to restore. No requests.")
        return ExecutionResult(requested_ids=())

    outcomes, interrupted = run_batches(
        client.save_albums,
        requested,
        noun="albums",
        verb="restoring",
        batch_size=batch_size,
        max_attempts=max_attempts,
        sleep=sleep,
        say=say,
    )

    result = ExecutionResult(
        batches=outcomes,
        requested_ids=requested,
        interrupted=interrupted,
    )
    say(
        f"Done: {result.removed_count} restored, {result.failed_count} failed, "
        f"{result.never_attempted_count} never attempted."
    )
    return result


def restore_command_for(path: Path | None) -> str:
    """The exact command that undoes this run."""
    if path is None:
        return ""
    return f"{RESTORE_COMMAND} {path}"
