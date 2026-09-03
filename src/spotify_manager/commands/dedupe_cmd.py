"""The `dedupe` subcommand.

At this stage the command is deliberately read-only and stops after the fetch: it
loads the library (cache or API) and prints what it found. Planning, the report, the
approval server and deletion arrive in later work and slot in after `load_library`.
"""

from __future__ import annotations

import argparse

from ..config import Config, load_config
from .library import load_library


def run(args: argparse.Namespace) -> int:
    config: Config = load_config()
    result = load_library(config, refresh=args.refresh, verbose=args.verbose)

    albums = result.snapshot.get("albums") or []
    source = (
        f"local snapshot, {_format_age(result.age_seconds)} old"
        if result.from_cache
        else "Spotify API"
    )

    print("Saved-album library")
    print(f"  source          {source}")
    print(f"  total albums    {len(albums)}")
    print(f"  pages fetched   {result.pages}")
    print(f"  requests made   {result.requests}")
    print(f"  elapsed         {result.elapsed_seconds:.2f}s")
    if result.from_cache:
        print()
        print("  Re-run with --refresh to bypass the snapshot and re-fetch.")
    else:
        print()
        print(f"  Snapshot written to {config.snapshot_path}")
    return 0


def _format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
