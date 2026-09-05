# ADR-0002: One batch-writer, shared by every feature that writes to Spotify

Status: accepted (2026-09-05)
Context: `like-album-tracks` (issue #10)

## Context

`dedupe/execute.py` grew a batch loop for deleting saved albums: fifty ids per request,
retry with exponential backoff, and a three-way classification of every batch as
`succeeded`, `failed`, or `never_attempted`. That classification is the most
load-bearing idea in the tool. The distinction between "we issued this and it errored,
so these ids are in an unknown state" and "we never issued this, so these ids are
certainly untouched" is the difference between telling a user the truth about their
library and telling them something plausible.

`like-album-tracks` writes to Spotify in exactly the same shape -- up to 215 batches of
fifty track ids -- and needs exactly the same honesty about what happened. The choice
was to copy that loop into the new feature, or to lift it out.

## Decision

The batch machinery lives in `spotify_manager/batching.py` and both features use it:
the status constants, `BatchOutcome`, the `BatchResult` classification, `batches_of`,
and the retry-and-issue loop. Features subclass `BatchResult` to add the one or two
fields their own record needs (`dedupe` adds the restore file's path; `likes` adds the
run record's) and to name the succeeded set in their own vocabulary (`removed_ids`,
`liked_ids`). Neither re-implements the classification.

What deliberately did **not** move is the recovery-file discipline. `dedupe` writes a
restore file before it deletes; `likes` writes a run record before it likes. Those are
each feature's own rule about its own irreversibility, not a general one, and each
stays in the module that owns the thing it must be able to undo. `batching.py` knows how
to issue ids and how to report what became of them, and nothing else.

The extraction was behaviour-preserving: the existing execution tests passed unchanged
apart from one constructor keyword. `BatchOutcome.album_ids` became `BatchOutcome.ids`,
because a batch of track ids is not a batch of album ids; every assertion in those tests
is untouched, and they are the regression net for this refactor.

## Consequences

**There is one place where the three-way classification can be got wrong, and one place
where fixing it fixes everything.** A second copy would have started identical and
drifted -- and the drift would not have been visible as a bug, only as two features
telling slightly different truths about the same kind of failure.

**Progress lines are parameterised** by noun and verb ("removing 50 albums", "liking 50
tracks"), which is the entire surface area of the difference between the two callers.

**`batching.py` is not pure and is not in either pure package.** It sleeps, and it reads
the shared HTTP module's backoff schedule. The purity tests that guard `dedupe/` and
`likes/` check that no pure module in either package reaches the I/O shell; `batching`
sits outside both, imported only by the two executors, which are the modules those tests
already name as the ones allowed to do I/O.
