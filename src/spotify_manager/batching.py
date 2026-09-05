"""Issuing a long list of ids to a write endpoint, and telling the truth about it.

Both features that write to Spotify -- removing saved albums, and liking the tracks
on them -- face exactly the same problem: a few hundred ids, an endpoint that takes
forty at a time, a network that fails transiently, and a user who has to be told
afterwards what state their library is actually in. That problem is solved once,
here, and the two features differ only in which call they hand over and what word
appears in the progress line.

**Every batch is classified, and nothing is summarised away.** A batch is exactly one
of:

* ``succeeded``       -- the API accepted it, and the write happened.
* ``failed``          -- it was issued and every retry was exhausted.
* ``never_attempted`` -- no request was ever issued for it, so those ids are
  certainly untouched.

The distinction between the last two is the entire point of this module. Collapsing
them into "some writes failed" would tell the user their library is in a state it is
not in.

A failed batch divides again, on the same principle. When every attempt came back as
a 4xx -- Spotify rejecting the request outright, as it does for a bad scope or an
account the app is not authorised for -- the write provably did not happen, and those
ids are as untouched as a never-attempted one. When the last attempt died any other
way -- a timeout, a dropped connection, a 5xx that outlived its retries -- the request
may well have applied before the error reached us, and those ids really are in an
unknown state. `BatchOutcome.rejected` tells the two apart, so a report can promise
"still saved" only where that is actually true.

**A failed batch does not abandon the following batches.** Batches are independent --
a rejected batch says nothing about the next one, and the failure that ends a run
midway is far more often transient than systemic. Abandoning the rest would convert a
small, precisely known failure into a large one. So a batch that exhausts its retries
is recorded as ``failed`` and the run continues. The only thing that stops the run is
an abrupt interrupt (Ctrl-C, a kill), which is a decision from outside the process and
must be obeyed immediately; everything after it is reported as ``never_attempted``,
because that is what it is.

Rate limiting is not handled here. `RateLimitedSession` already honours Spotify's own
`Retry-After` on a 429 -- mid-run exactly as anywhere else -- and backs off on 5xx, so
a throttled batch simply takes longer to return and then succeeds. The retry loop here
is the outer one, for the failures the session gives up on.

What is deliberately *not* here is any notion of a recovery file. Writing a restore
file before deleting albums is `dedupe`'s rule, and writing a run record before liking
tracks is `likes`' rule; each owns the thing it must be able to undo. This module
knows only how to issue ids and how to report what became of them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .errors import ApiError
from .infra.client import ID_BATCH_LIMIT
from .infra.http import backoff_delay

#: The three -- and only three -- things that can be true of a batch.
SUCCEEDED = "succeeded"
FAILED = "failed"
NEVER_ATTEMPTED = "never_attempted"

#: How many times one batch is issued before it is called failed. Each retry waits
#: longer than the last (see `backoff_delay`).
DEFAULT_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class BatchOutcome:
    """One request's worth of ids, and what became of it."""

    number: int
    ids: tuple[str, ...]
    status: str
    attempts: int = 0
    error: str | None = None
    #: HTTP status of the failure, when every attempt failed with one. None whenever
    #: any attempt died without a response to read a status from.
    error_status: int | None = None

    @property
    def size(self) -> int:
        return len(self.ids)

    @property
    def rejected(self) -> bool:
        """True when this batch failed and Spotify demonstrably applied none of it.

        A 4xx is a refusal to act, so the ids are untouched -- the same certainty a
        never-attempted batch carries, arrived at from the other direction.
        """
        return (
            self.status == FAILED
            and self.error_status is not None
            and 400 <= self.error_status < 500
        )


@dataclass(frozen=True)
class BatchResult:
    """What actually happened, batch by batch.

    Deliberately has no "succeeded" boolean and no "errors" list: every question a
    caller might ask is answered by classifying batches, so there is no way to read
    this object that quietly loses a failure.

    Features subclass this to add the one or two fields their own record needs; the
    classification itself is never re-implemented.
    """

    batches: tuple[BatchOutcome, ...] = ()
    requested_ids: tuple[str, ...] = ()
    interrupted: str | None = None

    def _ids(self, status: str) -> tuple[str, ...]:
        return tuple(i for b in self.batches if b.status == status for i in b.ids)

    @property
    def succeeded_ids(self) -> tuple[str, ...]:
        return self._ids(SUCCEEDED)

    @property
    def failed_ids(self) -> tuple[str, ...]:
        """Every id whose write was issued and errored, however it errored."""
        return self._ids(FAILED)

    @property
    def rejected_ids(self) -> tuple[str, ...]:
        """Failed ids Spotify refused outright (4xx): certainly untouched."""
        return tuple(i for b in self.batches if b.rejected for i in b.ids)

    @property
    def unknown_ids(self) -> tuple[str, ...]:
        """Failed ids whose write may or may not have applied: genuinely unknown."""
        return tuple(
            i for b in self.batches if b.status == FAILED and not b.rejected for i in b.ids
        )

    @property
    def never_attempted_ids(self) -> tuple[str, ...]:
        """Ids no request was ever issued for: certainly untouched."""
        return self._ids(NEVER_ATTEMPTED)

    @property
    def succeeded_count(self) -> int:
        return len(self.succeeded_ids)

    @property
    def failed_count(self) -> int:
        return len(self.failed_ids)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_ids)

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_ids)

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
        """True when every requested id was written. Nothing else is clean."""
        return self.succeeded_count == self.requested_count

    @property
    def nothing_requested(self) -> bool:
        return not self.requested_ids


def batches_of(ids: tuple[str, ...], size: int = ID_BATCH_LIMIT) -> Iterator[tuple[str, ...]]:
    """Split ids into the largest batches the endpoint permits.

    40 items per request is the maximum `/v1/me/library` accepts, so a 1281 album
    library costs at most 33 requests even if all of it were approved, and a 10,700
    track library at most 268.
    """
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def run_batches(
    call: Callable[[list[str]], None],
    ids: tuple[str, ...],
    *,
    noun: str,
    verb: str,
    batch_size: int = ID_BATCH_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = lambda _message: None,
) -> tuple[tuple[BatchOutcome, ...], str | None]:
    """Issue every id through `call`, in batches, and classify each batch.

    Args:
        call: the write to perform. Called once per batch with a list of ids, so the
            batching -- and therefore the classification -- is decided here.
        ids: everything to write, in the order it should go out.
        noun: what the ids are, for the progress line ("albums", "tracks").
        verb: what is being done to them ("removing", "restoring", "liking").
        batch_size: ids per batch, and therefore per request. May not exceed
            `ID_BATCH_LIMIT`; see the ValueError below.
        max_attempts: total issues of one batch before it is called failed.
        sleep: injected so the backoff schedule can be asserted without waiting.
        say: called with one line per notable step, for the terminal.

    Returns:
        (every batch's outcome in order, the interrupt reason or None). Every id
        appears in exactly one classification.

    Raises:
        ValueError: if `batch_size` exceeds `ID_BATCH_LIMIT`, which would make one
            batch more than one request and the classification a guess.
    """
    if batch_size > ID_BATCH_LIMIT:
        # One batch must be one request, or the classification below is a guess. The
        # client re-chunks anything larger at `ID_BATCH_LIMIT`, so a batch of 80
        # would be two requests reported as one: if the second exhausted its retries,
        # all 80 ids would be called failed, including the 40 that demonstrably
        # succeeded. Refuse rather than report that.
        raise ValueError(
            f"batch_size {batch_size} exceeds the {ID_BATCH_LIMIT} items one request "
            "may carry; a batch that is more than one request cannot be classified."
        )
    planned = list(batches_of(ids, batch_size))
    outcomes: list[BatchOutcome] = []
    interrupted: str | None = None

    for index, batch in enumerate(planned):
        number = index + 1
        if interrupted is not None:
            # No request was issued for this batch, and now none ever will be. Saying
            # "failed" here would be a lie in the dangerous direction.
            outcomes.append(BatchOutcome(number=number, ids=batch, status=NEVER_ATTEMPTED))
            continue
        outcome, interrupted = issue_batch(
            call,
            batch,
            number=number,
            total=len(planned),
            noun=noun,
            verb=verb,
            max_attempts=max_attempts,
            sleep=sleep,
            say=say,
        )
        outcomes.append(outcome)

    return tuple(outcomes), interrupted


def issue_batch(
    call: Callable[[list[str]], None],
    batch: tuple[str, ...],
    *,
    number: int,
    total: int,
    noun: str,
    verb: str,
    max_attempts: int,
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> tuple[BatchOutcome, str | None]:
    """Issue one batch, retrying with increasing delays. Returns (outcome, interrupt).

    The second element is None unless the run was interrupted from outside, in which
    case it carries the reason and the caller must issue nothing more.

    The delay before attempt *n* is `backoff_delay(n - 1)`: 1s, 2s, 4s, ... The wait
    happens inside the same guarded block as the request, so an interrupt arriving
    while we are waiting is classified exactly like one arriving mid-request.

    A failure is recorded as a refusal (`BatchOutcome.rejected`) only when *every*
    attempt came back 4xx. One attempt that died without a status -- a timeout, a
    reset -- is enough to make the batch's state unknown, because that attempt may
    have reached Spotify and applied.
    """
    last_error: str | None = None
    statuses: list[int | None] = []
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                wait = backoff_delay(attempt - 1, jitter=0.25)
                say(f"Batch {number} failed ({last_error}); retrying in {wait:.1f}s.")
                sleep(wait)
            say(
                f"Batch {number}/{total}: {verb} {len(batch)} {noun} "
                f"(attempt {attempt}/{max_attempts})."
            )
            call(list(batch))
        except Exception as exc:  # noqa: BLE001 - any failure is this batch's failure
            last_error = f"{type(exc).__name__}: {exc}"
            statuses.append(exc.status if isinstance(exc, ApiError) else None)
            if attempt < max_attempts:
                continue
            say(f"Batch {number} failed after {attempt} attempts: {last_error}")
            return (
                BatchOutcome(
                    number=number,
                    ids=batch,
                    status=FAILED,
                    attempts=attempt,
                    error=last_error,
                    error_status=_settled_status(statuses),
                ),
                None,
            )
        except BaseException as exc:  # Ctrl-C, SystemExit: obey it, then report honestly.
            # A request may already have been sent, so these ids are in an unknown
            # state: failed, never "untouched".
            reason = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
            say(f"Interrupted during batch {number}. Issuing nothing further.")
            return (
                BatchOutcome(
                    number=number,
                    ids=batch,
                    status=FAILED,
                    attempts=attempt,
                    error=f"interrupted before this batch was confirmed ({reason})",
                ),
                reason,
            )
        return (
            BatchOutcome(number=number, ids=batch, status=SUCCEEDED, attempts=attempt),
            None,
        )
    raise AssertionError("unreachable: the loop returns on every path")  # pragma: no cover


def _settled_status(statuses: list[int | None]) -> int | None:
    """The status to record for a batch every attempt at which failed.

    Returns the last status only if every attempt reported a 4xx; otherwise None,
    which reads downstream as "we do not know what became of this batch". Erring
    towards None is the safe direction: it claims less.
    """
    if statuses and all(s is not None and 400 <= s < 500 for s in statuses):
        return statuses[-1]
    return None
