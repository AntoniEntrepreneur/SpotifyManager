"""Argument parsing and top-level error handling.

Deliberate failures (`SpotifyManagerError` and its subclasses) are printed as plain
messages and exit 1. Only genuine bugs produce a traceback.
"""

from __future__ import annotations

import argparse
import sys

from . import report
from .commands import dedupe_cmd, like_cmd, redact_cmd, restore_cmd, unlike_cmd
from .errors import SpotifyManagerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-manager",
        description="Manage a personal Spotify library from the command line.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dedupe = subparsers.add_parser(
        "dedupe",
        help="review saved albums for duplicate editions and approve a cleanup",
        description=(
            "Fetch the saved-album library (from the local snapshot when it is still "
            "fresh), work out which albums are duplicate editions of each other, and "
            "then serve a report showing every group, its members, the proposed "
            "keeper, the key that grouped them and the decorations that were ignored "
            "to match. Tick exactly which albums to keep, skip any group you "
            "disagree with, and approve. Skipped groups are remembered as pairs of "
            "albums judged not to be duplicates, and those comparisons are never "
            "proposed again until --clear-decisions forgets them. The run waits for "
            "that decision with no timeout, and can be abandoned with Ctrl-C. "
            "Approving carries the decisions out: a timestamped restore file is "
            "written before anything is removed, the albums are deleted in batches, "
            "and the page becomes a results view stating exactly what succeeded, "
            "what failed and what was never attempted."
        ),
    )
    dedupe.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the local snapshot and fetch the library from Spotify",
    )
    dedupe.add_argument(
        "--no-browser",
        action="store_true",
        help="serve the report without opening it in a browser",
    )
    dedupe.add_argument(
        "--port",
        type=int,
        default=report.DEFAULT_PORT,
        metavar="PORT",
        help=(
            "local port to serve the approval page on "
            f"(default: {report.DEFAULT_PORT}; the page is bound to 127.0.0.1 only)"
        ),
    )
    dedupe.add_argument(
        "--clear-decisions",
        action="store_true",
        help=(
            "forget every 'not duplicates' pair recorded by earlier runs, so groups "
            "suppressed by those decisions are proposed again"
        ),
    )
    dedupe.add_argument(
        "--verbose",
        action="store_true",
        help="print every API request to stderr",
    )
    dedupe.set_defaults(func=dedupe_cmd.run)

    redact = subparsers.add_parser(
        "redact-snapshot",
        help="write a redacted copy of the local snapshot for use as test fixture data",
        description=(
            "Read the cached library snapshot and write a redacted copy suitable for "
            "committing as test fixture data. Album, artist and release metadata "
            "needed by the planner is preserved; per-account detail is stripped."
        ),
    )
    redact_cmd.add_arguments(redact)
    redact.set_defaults(func=redact_cmd.run)

    like = subparsers.add_parser(
        "like-album-tracks",
        help="like every song on every saved album that is not liked yet",
        description=(
            "Work out every track on every saved album that is not already in Liked "
            "Songs, and like it. Track lists come from the saved-album snapshot the "
            "tool already holds, so discovery costs no requests; only an album whose "
            "listed track page was truncated is fetched individually. The same "
            "recording saved on two editions of one record is liked once, from the "
            "richer edition, never from a compilation when a real album has it. The "
            "plan is shown as counts and nothing is written until you confirm. "
            "Before the first like is issued, a run record naming exactly the tracks "
            "the run set out to like is written to disk, so the run can be undone; "
            "if that record cannot be written, nothing is liked. Likes go out in "
            "batches of forty, oldest saved album first, and the command reports "
            "exactly how many tracks were liked, failed, or never attempted. "
            "Re-running finishes an interrupted run: it likes only what is still "
            "missing."
        ),
    )
    like_cmd.add_arguments(like)
    like.set_defaults(func=like_cmd.run)

    restore = subparsers.add_parser(
        "restore",
        help="re-save every album listed in a restore file",
        description=(
            "Read a restore file written by `dedupe`, fully parse and validate it, "
            "and re-save every album it lists, in the largest batches the API "
            "permits. A missing or malformed restore file produces a clear message "
            "and restores nothing; re-saving an album that is already saved is "
            "harmless. Throttling and transient errors are handled the same way "
            "they are during deletion, and the command reports exactly how many "
            "albums were restored, failed, or never attempted."
        ),
    )
    restore_cmd.add_arguments(restore)
    restore.set_defaults(func=restore_cmd.run)

    unlike = subparsers.add_parser(
        "unlike-tracks",
        help="remove every like listed in a run record written by `like-album-tracks`",
        description=(
            "Read a run record written by `like-album-tracks`, fully parse and "
            "validate it, and remove the like from every track it lists, in the "
            "largest batches the API permits. This is how a like run is undone: the "
            "record names exactly the tracks that run set out to like, which is the "
            "only thing that tells them apart from likes made by hand. The count is "
            "shown and nothing is removed until you confirm, because the record's "
            "contents were chosen by the planner rather than by you: a track you had "
            "already liked by hand can sit in one as a like the run never actually "
            "made. A missing or "
            "malformed run record produces a clear message and unlikes nothing; "
            "removing a like that is not there is harmless, so a record listing more "
            "than the run managed costs nothing. The record file is read, not "
            "consumed -- re-running `like-album-tracks` is how this run is itself "
            "undone -- though the original Liked Songs dates cannot be brought back. "
            "Throttling and transient errors are handled the same way they are during "
            "liking, and the command reports exactly how many tracks were unliked, "
            "failed, or never attempted."
        ),
    )
    unlike_cmd.add_arguments(unlike)
    unlike.set_defaults(func=unlike_cmd.run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SpotifyManagerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # A last-resort net: an interrupt during a phase that has no cleaner answer
        # of its own still ends as a sentence, not a traceback.
        print("\nAborted. Nothing was changed.", file=sys.stderr)
        return 130
