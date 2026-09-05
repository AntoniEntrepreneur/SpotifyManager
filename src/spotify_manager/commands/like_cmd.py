"""The `like-album-tracks` subcommand: like every track on every saved album.

The shape of this command follows from what it does. It adds rather than destroys,
so there is no browser review -- and a review page of ten thousand rows would not be
read by anyone anyway. What it does instead is show the plan as counts, wait for a
typed yes, and then hand the ids to `likes.execute.like`, which writes the run record
before it writes anything to Spotify.

Three rules here are worth stating, because each one exists to prevent a specific
accident:

* **A non-terminal stdin without `--yes` refuses.** Not proceeds, and not blocks. A
  redirect must never be able to approve a ten-thousand-row mutation on the user's
  behalf, and a CI job must never hang on a prompt nobody can answer.
* **`--dry-run` writes nothing at all**, run record included. A record file for a run
  that did not happen would tell a future `unlike` to remove likes this tool never
  added.
* **The Liked Songs snapshot is deleted on the way out, however the run ended.** The
  run has just invalidated it. Deleting rather than updating is the only honest
  option, because a failed batch leaves tracks in a state this process does not know.
"""

from __future__ import annotations

import argparse

from ..config import Config, load_config
from ..likes.execute import LikeResult, like
from ..likes.models import liked_ids_from_raw, library_from_raw
from ..likes.planner import format_plan, plan_likes
from .confirm import confirmed as ask
from .library import (
    build_client,
    complete_album_tracks,
    liked_tracks_cache,
    load_library,
    load_liked_tracks,
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="bypass both local snapshots and fetch the library from Spotify",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (required when stdin is not a terminal)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit without liking anything or writing any file",
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
    config: Config = load_config()
    client = build_client(config, verbose=args.verbose, rate=args.rate)

    loaded = load_library(config, refresh=args.refresh, verbose=args.verbose)
    print(_source_line("Saved albums", loaded))

    library = complete_album_tracks(
        library_from_raw(loaded.snapshot), client, on_progress=_progress
    )

    liked = load_liked_tracks(
        config, refresh=args.refresh, verbose=args.verbose, client=client
    )
    print(_source_line("Liked Songs", liked))

    plan = plan_likes(library, liked_ids_from_raw(liked.snapshot))
    print()
    print(format_plan(plan))

    if plan.nothing_to_do:
        return 0
    if args.dry_run:
        print()
        print("Dry run: nothing was liked and no run record was written.")
        return 0
    if not confirmed(plan.to_like_count, assume_yes=args.yes):
        print("Nothing was liked.")
        return 1

    print()
    try:
        result = like(
            plan.track_ids,
            client,
            likes_dir=config.likes_dir,
            on_progress=_progress,
        )
    finally:
        # The run touched Liked Songs, so the snapshot is now wrong however this
        # ended -- including by Ctrl-C. Deleting it makes the next run refetch and
        # do exactly the work that is still outstanding, which is what "resuming"
        # means here.
        liked_tracks_cache(config).discard()

    print()
    print(format_results(result))
    return 0 if result.is_clean else 1


def confirmed(count: int, *, assume_yes: bool, stream=None) -> bool:
    """Whether the user has actually agreed to like `count` tracks.

    The rules -- and the reasons for them -- are `commands.confirm`'s, shared with
    `unlike-tracks`; this only supplies the wording.
    """
    return ask(
        f"Like {count} tracks?",
        refusal="Refusing to like tracks without confirmation: stdin is not a terminal.",
        assume_yes=assume_yes,
        stream=stream,
    )


def format_results(result: LikeResult) -> str:
    """What the run did, in the terminal, with nothing summarised away.

    The same shape as `dedupe`'s and `restore`'s results, because a reader who has
    seen one recognises the others: requested, succeeded, failed, never attempted.
    """
    lines = [
        "Results",
        f"  requested to like      {result.requested_count}",
        f"  liked                  {result.liked_count}",
        f"  failed                 {result.failed_count}",
        f"  never attempted        {result.never_attempted_count}",
    ]
    if result.record_path is not None:
        lines += ["", f"Run record: {result.record_path}"]
    if result.is_clean:
        lines += ["", "Every track was liked."]
        return "\n".join(lines)

    lines += ["", "THIS RUN DID NOT FINISH. Some tracks may not have been liked."]
    lines += _listing(
        "Refused by Spotify",
        result.rejected_ids,
        "Spotify rejected these requests outright; those tracks were not liked",
    )
    lines += _listing(
        "Failed",
        result.unknown_ids,
        "these requests were sent and errored; those tracks may or may not be liked",
    )
    lines += _listing(
        "Never attempted",
        result.never_attempted_ids,
        "no request was ever issued for these; they were not liked",
    )
    for batch in result.failed_batches:
        lines += ["", f"Batch {batch.number} error: {batch.error}"]
    lines += ["", "Re-run the command to finish: it likes only what is still missing."]
    return "\n".join(lines)


def _listing(title: str, ids: tuple[str, ...], note: str) -> list[str]:
    if not ids:
        return []
    shown = ids[:50]
    lines = ["", f"{title} ({len(ids)}) -- {note}"] + [f"  {track_id}" for track_id in shown]
    if len(ids) > len(shown):
        lines.append(f"  ... and {len(ids) - len(shown)} more (see the run record)")
    return lines


def _source_line(what: str, loaded) -> str:
    if loaded.from_cache:
        return f"{what}: from the local snapshot ({loaded.age_seconds / 3600:.1f}h old)."
    return (
        f"{what}: fetched {loaded.pages} page(s) in {loaded.requests} request(s), "
        f"{loaded.elapsed_seconds:.1f}s."
    )


def _progress(line: str) -> None:
    print(line, flush=True)
