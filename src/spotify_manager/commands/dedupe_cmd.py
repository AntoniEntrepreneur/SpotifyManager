"""The `dedupe` subcommand.

The command is still read-only: it loads the library (cache or API), computes the
plan, and prints it. Nothing is deleted, and nothing can be -- the report, the
approval server and the deletion path arrive in later work and slot in after
`plan`.

The printing lives here rather than in the planner because the planner must stay a
pure function returning data; how that data is shown is a presentation choice.
"""

from __future__ import annotations

import argparse

from ..config import Config, load_config
from ..dedupe.models import DedupePlan, DuplicateGroup, snapshot_from_raw
from ..dedupe.planner import plan as build_plan
from .library import load_library


def run(args: argparse.Namespace) -> int:
    config: Config = load_config()
    result = load_library(config, refresh=args.refresh, verbose=args.verbose)

    source = (
        f"local snapshot, {_format_age(result.age_seconds)} old"
        if result.from_cache
        else "Spotify API"
    )
    snapshot = snapshot_from_raw(result.snapshot)
    dedupe_plan = build_plan(snapshot)

    print("Saved-album library")
    print(f"  source          {source}")
    print(f"  pages fetched   {result.pages}")
    print(f"  requests made   {result.requests}")
    print(f"  elapsed         {result.elapsed_seconds:.2f}s")
    print()
    print(format_plan(dedupe_plan))
    print()
    print("Nothing has been changed. Reviewing and approving arrives in later work.")
    return 0


def format_plan(dedupe_plan: DedupePlan) -> str:
    """Render a plan as plain text: every group, its members, keeper and reasoning."""
    lines = [_summary_line(dedupe_plan)]
    for reason, count in sorted(dedupe_plan.excluded_reasons.items()):
        lines.append(f"Excluded from analysis: {count} ({reason}).")
    if dedupe_plan.suppressed_group_count:
        lines.append(
            f"{dedupe_plan.suppressed_group_count} group(s) suppressed by earlier "
            "'not duplicates' decisions."
        )
    if not dedupe_plan.groups:
        lines.append("")
        lines.append("No duplicate editions found.")
        return "\n".join(lines)

    for index, group in enumerate(dedupe_plan.groups, start=1):
        lines.append("")
        lines.extend(_format_group(index, group))
    return "\n".join(lines)


def _summary_line(dedupe_plan: DedupePlan) -> str:
    return (
        f"Scanned {dedupe_plan.total_albums_scanned} albums. "
        f"{dedupe_plan.albums_in_groups} album(s) across "
        f"{len(dedupe_plan.groups)} duplicate group(s); "
        f"{dedupe_plan.proposed_removal_count} proposed for removal."
    )


def _format_group(index: int, group: DuplicateGroup) -> list[str]:
    lines = [
        f'Group {index}: key="{group.key.normalized_title}" | '
        f"artist={group.key.primary_artist_id} | type={group.key.album_type}",
        f"  artist: {group.keeper.album.artist_names}",
    ]
    ignored = ", ".join(group.ignored_decorations) if group.ignored_decorations else "(none)"
    lines.append(f"  ignored decorations: {ignored}")
    if group.suppressed_pair_count:
        lines.append(
            f"  {group.suppressed_pair_count} comparison(s) already judged not duplicates"
        )
    width = max(len(m.album.name) for m in group.members)
    for member in group.members:
        marker = "[KEEP]  " if member.is_keeper else "[remove]"
        lines.append(
            f"  {marker} {member.album.name:<{width}}  "
            f"{member.edition_category}({member.edition_rank})  "
            f"tracks={member.album.total_tracks}  {member.album.release_date}  "
            f"{member.album.id}"
        )
    return lines


def _format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
