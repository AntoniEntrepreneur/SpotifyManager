"""Argument parsing and top-level error handling.

Deliberate failures (`SpotifyManagerError` and its subclasses) are printed as plain
messages and exit 1. Only genuine bugs produce a traceback.
"""

from __future__ import annotations

import argparse
import sys

from . import report
from .commands import dedupe_cmd, redact_cmd
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
            "disagree with, and approve. The run waits for that decision with no "
            "timeout, and can be abandoned with Ctrl-C. Approving carries the "
            "decisions out: a timestamped restore file is written before anything "
            "is removed, the albums are deleted in batches, and the page becomes a "
            "results view stating exactly what succeeded, what failed and what was "
            "never attempted."
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
