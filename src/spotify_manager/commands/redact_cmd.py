"""The `redact-snapshot` subcommand: turn the local snapshot into a test fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import state_dir
from ..errors import SpotifyManagerError
from ..tools.redact import DEFAULT_OUTPUT, redact_file


def run(args: argparse.Namespace) -> int:
    source = Path(args.source) if args.source else state_dir() / "library_snapshot.json"
    destination = Path(args.output)

    if not source.exists():
        raise SpotifyManagerError(
            f"No library snapshot at {source}.\n"
            "Run `spotify-manager dedupe` first to fetch and cache your library."
        )

    count = redact_file(source, destination)
    print(f"Redacted {count} albums from {source}")
    print(f"Wrote {destination}")
    print("See spotify_manager.tools.redact for exactly what is kept and stripped.")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        default=None,
        help="snapshot to redact (default: the cached snapshot in the state directory)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"where to write the redacted fixture (default: {DEFAULT_OUTPUT})",
    )
