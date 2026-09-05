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

**Every batch is classified, and nothing is summarised away.** A batch is exactly one
of:

* ``succeeded``  -- the API accepted it, and those albums are gone.
* ``failed``     -- it was issued, every retry was exhausted, and the albums in it are
  in an *unknown* state (the request may have applied before the error).
* ``never_attempted`` -- no request was ever issued for it, so those albums are
  certainly still in the library.

The distinction between the last two is the entire point of this module. Collapsing
them into "some deletions failed" would tell the user their library is in a state it
is not in.

**A failed batch does not abandon the following batches.** Batches are independent --
a rejected batch says nothing about the next one, and the failure that ends a run
midway is far more often transient than systemic. Abandoning the rest would convert a
small, precisely known failure into a large one, and the user would have to re-run and
re-review to finish work they already approved. So a batch that exhausts its retries
is recorded as ``failed`` and the run continues. The only thing that stops the run is
an abrupt interrupt (Ctrl-C, a kill), which is a decision from outside the process and
must be obeyed immediately; everything after it is reported as ``never_attempted``,
because that is what it is.

Rate limiting is not handled here. `RateLimitedSession` already honours Spotify's own
`Retry-After` on a 429 -- mid-deletion exactly as anywhere else -- and backs off on
5xx, so a throttled batch simply takes longer to return and then succeeds. The retry
loop here is the outer one, for the failures the session gives up on.

`restore` is the mirror image of `execute`, undoing exactly what a restore file
promises: it shares the batching, the retry-with-backoff and the three-way
classification, because a restore run can fail in every way a deletion run can and
deserves the same honesty about it. It has no restore file of its own to write --
the file it is restoring *from* is already that record -- and it does not need any
special handling for an album that turns out to already be saved, because PUT
`/v1/me/albums` is idempotent on Spotify's side: re-saving a saved album is a
no-op, not an error, so it is simply reported as succeeded like any other.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import SpotifyManagerError
from ..infra.client import ID_BATCH_LIMIT
from ..infra.http import backoff_delay
from .resolve import Resolution

#: The three -- and only three -- things that can be true of a batch.
SUCCEEDED = "succeeded"
FAILED = "failed"
NEVER_ATTEMPTED = "never_attempted"

#: How many times one batch is issued before it is called failed. Each retry waits
#: longer than the last (see `backoff_delay`).
DEFAULT_MAX_ATTEMPTS = 4

#: Filename stamp shared by the restore file and the archived results report, so the
#: two halves of one run's record sort together and are obviously the same run.
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"

#: What the results view tells the user to type to undo this run.
RESTORE_COMMAND = "spotify-manager restore"


class RestoreFileError(SpotifyManagerError):
    """The restore file could not be written, so nothing may be deleted."""


@dataclass(frozen=True)
class BatchOutcome:
    """One request's worth of album ids, and what became of it."""

    number: int
    album_ids: tuple[str, ...]
    status: str
    attempts: int = 0
    error: str | None = None

    @property
    def size(self) -> int:
        return len(self.album_ids)


@dataclass(frozen=True)
class ExecutionResult:
    """What actually happened, batch by batch. The only source for the results view.

    Deliberately has no "succeeded" boolean and no "errors" list: every question a
    caller might ask is answered by classifying batches, so there is no way to read
    this object that quietly loses a failure.
    """

    batches: tuple[BatchOutcome, ...] = ()
    requested_ids: tuple[str, ...] = ()
    restore_path: Path | None = None
    created_at: str = ""
    interrupted: str | None = None

    def _ids(self, status: str) -> tuple[str, ...]:
        return tuple(i for b in self.batches if b.status == status for i in b.album_ids)

    @property
    def removed_ids(self) -> tuple[str, ...]:
        return self._ids(SUCCEEDED)

    @property
    def failed_ids(self) -> tuple[str, ...]:
        """Albums whose removal was issued and errored: state unknown, not 'kept'."""
        return self._ids(FAILED)

    @property
    def never_attempted_ids(self) -> tuple[str, ...]:
        """Albums no request was ever issued for: certainly still in the library."""
        return self._ids(NEVER_ATTEMPTED)

    @property
    def removed_count(self) -> int:
        return len(self.removed_ids)

    @property
    def failed_count(self) -> int:
        return len(self.failed_ids)

    @property
    def never_attempted_count(self) -> int:
        return len(self.never_attempted_ids)

    @property
    def requested_count(self) -> int:
        return len(self.requested_ids)

    @property
    def failed_batches(self) -> tuple[BatchOutcome, ...]:
        return tuple(b for b in self.batches if b.status == FAILED)

    @property
    def is_clean(self) -> bool:
        """True when every album approved for removal is gone. Nothing else is clean."""
        return self.removed_count == self.requested_count

    @property
    def nothing_requested(self) -> bool:
        return not self.requested_ids


@dataclass(frozen=True)
class Applied:
    """A decision that has been carried out: what was approved, and what happened.

    The pair travels together because neither half is readable alone -- the result
    holds ids, and the resolution holds the album names those ids belong to.
    """

    resolution: Resolution
    result: ExecutionResult
    recorded_pairs: int = 0


def batches_of(ids: tuple[str, ...], size: int = ID_BATCH_LIMIT) -> Iterator[tuple[str, ...]]:
    """Split ids into the largest batches the endpoint permits.

    50 ids per DELETE is the documented maximum for the JSON-body form, so a 1281
    album library costs at most 26 requests even if all of it were approved.
    """
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


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

    planned = list(batches_of(requested, batch_size))
    outcomes: list[BatchOutcome] = []
    interrupted: str | None = None

    for index, batch in enumerate(planned):
        number = index + 1
        if interrupted is not None:
            # No request was issued for this batch, and now none ever will be. Saying
            # "failed" here would be a lie in the dangerous direction.
            outcomes.append(
                BatchOutcome(number=number, album_ids=batch, status=NEVER_ATTEMPTED)
            )
            continue
        outcome, interrupted = _issue_batch(
            client.delete_albums,
            batch,
            number=number,
            total=len(planned),
            verb="removing",
            max_attempts=max_attempts,
            sleep=sleep,
            say=say,
        )
        outcomes.append(outcome)

    result = ExecutionResult(
        batches=tuple(outcomes),
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

    planned = list(batches_of(requested, batch_size))
    outcomes: list[BatchOutcome] = []
    interrupted: str | None = None

    for index, batch in enumerate(planned):
        number = index + 1
        if interrupted is not None:
            outcomes.append(
                BatchOutcome(number=number, album_ids=batch, status=NEVER_ATTEMPTED)
            )
            continue
        outcome, interrupted = _issue_batch(
            client.save_albums,
            batch,
            number=number,
            total=len(planned),
            verb="restoring",
            max_attempts=max_attempts,
            sleep=sleep,
            say=say,
        )
        outcomes.append(outcome)

    result = ExecutionResult(
        batches=tuple(outcomes),
        requested_ids=requested,
        interrupted=interrupted,
    )
    say(
        f"Done: {result.removed_count} restored, {result.failed_count} failed, "
        f"{result.never_attempted_count} never attempted."
    )
    return result


def _issue_batch(
    call: Callable[[list[str]], None],
    batch: tuple[str, ...],
    *,
    number: int,
    total: int,
    verb: str,
    max_attempts: int,
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> tuple[BatchOutcome, str | None]:
    """Issue one batch, retrying with increasing delays. Returns (outcome, interrupt).

    Shared by `execute` (DELETE, `verb="removing"`) and `restore` (PUT,
    `verb="restoring"`): the batching, retry and classification are exactly the same
    shape either direction, only the request itself and the word in the progress
    line differ.

    The second element is None unless the run was interrupted from outside, in which
    case it carries the reason and the caller must issue nothing more.

    The delay before attempt *n* is `backoff_delay(n - 1)`: 1s, 2s, 4s, ... The wait
    happens inside the same guarded block as the request, so an interrupt arriving
    while we are waiting is classified exactly like one arriving mid-request.
    """
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                wait = backoff_delay(attempt - 1, jitter=0.25)
                say(f"Batch {number} failed ({last_error}); retrying in {wait:.1f}s.")
                sleep(wait)
            say(
                f"Batch {number}/{total}: {verb} {len(batch)} albums "
                f"(attempt {attempt}/{max_attempts})."
            )
            call(list(batch))
        except Exception as exc:  # noqa: BLE001 - any failure is this batch's failure
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                continue
            say(f"Batch {number} failed after {attempt} attempts: {last_error}")
            return (
                BatchOutcome(
                    number=number,
                    album_ids=batch,
                    status=FAILED,
                    attempts=attempt,
                    error=last_error,
                ),
                None,
            )
        except BaseException as exc:  # Ctrl-C, SystemExit: obey it, then report honestly.
            # A request may already have been sent, so these albums are in an unknown
            # state: failed, never "still there".
            reason = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
            say(f"Interrupted during batch {number}. Issuing nothing further.")
            return (
                BatchOutcome(
                    number=number,
                    album_ids=batch,
                    status=FAILED,
                    attempts=attempt,
                    error=f"interrupted before this batch was confirmed ({reason})",
                ),
                reason,
            )
        return (
            BatchOutcome(
                number=number, album_ids=batch, status=SUCCEEDED, attempts=attempt
            ),
            None,
        )
    raise AssertionError("unreachable: the loop returns on every path")  # pragma: no cover


def restore_command_for(path: Path | None) -> str:
    """The exact command that undoes this run."""
    if path is None:
        return ""
    return f"{RESTORE_COMMAND} {path}"
