"""The `unlike-tracks` subcommand: undo a like run from its run record.

`like-album-tracks` writes a run record before it likes anything, naming exactly the
tracks that run set out to like. This command spends it. Nothing in Spotify
distinguishes a track this tool liked from one the user liked by hand three years
ago, so that file is the only thing that can tell the two apart -- which is why this
command takes a path and never a guess.

Its shape is `restore`'s, one noun over, and for the same reason: the file is read and
fully parsed and validated *before* a single request goes out, so a missing or
malformed record produces a clear message and touches nothing. Past that point the
ids go out with the same batching, retries and honest three-way classification every
other write in this tool uses.

Two things are deliberately not here. **No record file of its own** -- the file being
undone is already that record, and it is read rather than consumed, because re-liking
is how this run is itself undone. **No confirmation prompt** -- unlike
`like-album-tracks`, which decides for itself what to touch, this command was handed
an explicit list by an explicit path; the user has already named exactly what they
mean.

What it cannot undo is chronology. Liked Songs is ordered by when each track was
liked, and un-liking then re-liking a track stamps it with the moment of the re-like.
That date is gone either way, and no file can bring it back.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import Config, load_config
from ..errors import SpotifyManagerError
from ..likes.execute import UnlikeResult, unlike
from ..likes.record import LikeRecord, RecordDocumentError
from .library import build_client, liked_tracks_cache


class RecordFileNotFoundError(SpotifyManagerError):
    """The given path does not name a file this command can read."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "run_record",
        type=Path,
        metavar="RUN_RECORD",
        help="path to a run record written by `like-album-tracks`",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        metavar="PER_SECOND",
        help=(
            "override the client-side ceiling on requests per second for this run "
            "(default: the shared session's own limit)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every API request to stderr",
    )


def run(args: argparse.Namespace) -> int:
    # The file is read and validated before anything else -- including credentials
    # -- so a bad run record fails the same clear way whether or not Spotify login is
    # even configured, and never as a side effect of reaching the API.
    record = load_record_file(args.run_record)
    config: Config = load_config()

    if not record.track_ids:
        # A run record with an empty list is not malformed -- `like-album-tracks`
        # never writes one, but nothing rules it out -- it just names nothing to do.
        # The Liked Songs snapshot is left alone: no request went out, so it is still
        # exactly right, and discarding it would cost a refetch to learn nothing.
        print(f"{args.run_record} lists no tracks. Nothing was unliked.")
        return 0

    print(f"Unliking {len(record.track_ids)} track(s) from {args.run_record}.")
    # Built outside the try on purpose: a client that cannot be built has issued no
    # request, so Liked Songs is still exactly what the snapshot says it is, and the
    # `finally` below must not throw it away for nothing.
    client = build_client(config, verbose=args.verbose, rate=args.rate)
    try:
        result = unlike(
            record.track_ids,
            client,
            on_progress=lambda line: print(line, flush=True),
        )
    finally:
        # The run touched Liked Songs, so the snapshot is now wrong however this
        # ended -- including by Ctrl-C. Deleting rather than updating is the only
        # honest option: a failed batch leaves tracks in a state this process does
        # not know.
        liked_tracks_cache(config).discard()

    print()
    print(format_result(result))
    # A run that did not finish did not succeed, and should not tell a shell script
    # that it did.
    return 0 if result.is_clean else 1


def load_record_file(path: Path) -> LikeRecord:
    """Read and completely validate the run record before anything is sent.

    Reading, JSON-decoding and schema validation all happen here, in that order,
    before the first `remove_tracks` call -- so any of the three ways a run record can
    be wrong (missing, not JSON, wrong shape) ends the run the same way: a clear
    message, nothing unliked.

    Raises:
        RecordFileNotFoundError: the path cannot be read.
        RecordDocumentError: the contents are not valid JSON, or not a valid run
            record.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordFileNotFoundError(
            f"Could not read run record {path}: {exc}. Nothing was unliked."
        ) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RecordDocumentError(
            f"{path} is not valid JSON ({exc}). Nothing was unliked."
        ) from exc
    return LikeRecord.from_raw(raw)


def format_result(result: UnlikeResult) -> str:
    """What the run did, in the terminal, with nothing summarised away.

    The same shape as `dedupe`'s, `restore`'s and `like-album-tracks`'s results,
    because a reader who has seen one recognises the others: requested, succeeded,
    failed, never attempted.
    """
    lines = [
        "Results",
        f"  requested to unlike    {result.requested_count}",
        f"  unliked                {result.unliked_count}",
        f"  failed                 {result.failed_count}",
        f"  never attempted        {result.never_attempted_count}",
    ]
    if result.is_clean:
        lines += ["", "Every track was unliked."]
        return "\n".join(lines)

    lines += ["", "THIS RUN DID NOT FINISH. Some tracks may not have been unliked."]
    lines += _listing(
        "Failed",
        result.failed_ids,
        "these requests were sent and errored; those tracks may or may not be liked",
    )
    lines += _listing(
        "Never attempted",
        result.never_attempted_ids,
        "no request was ever issued for these; they were not unliked",
    )
    for batch in result.failed_batches:
        lines += ["", f"Batch {batch.number} error: {batch.error}"]
    lines += [
        "",
        "Re-run the command with the same run record to finish: removing a like that "
        "is already gone is a no-op.",
    ]
    return "\n".join(lines)


def _listing(title: str, ids: tuple[str, ...], note: str) -> list[str]:
    """One classification's ids, truncated -- a run record can list ten thousand.

    The same truncation `like-album-tracks` uses, for the same reason: the file
    naming every id is still on disk, and the terminal is not where it is read.
    """
    if not ids:
        return []
    shown = ids[:50]
    lines = ["", f"{title} ({len(ids)}) -- {note}"] + [f"  {track_id}" for track_id in shown]
    if len(ids) > len(shown):
        lines.append(f"  ... and {len(ids) - len(shown)} more (see the run record)")
    return lines
