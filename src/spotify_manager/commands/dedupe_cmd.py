"""The `dedupe` subcommand: plan, then approve, then -- for now -- stop.

The run loads the library (cache or API), computes the plan, prints it, archives a
read-only copy of the report, then serves an interactive copy on a loopback port and
blocks until the reviewer submits their decisions. Those decisions are resolved into
an exact set of album ids, which is printed and nothing more. No album is deleted,
no restore file is written and no ledger is updated: executing the resolution and
recording the skipped groups are separate pieces of work, and keeping them separate
is the point -- the mapping from ticks to deletions is built and verified with
nothing irreversible attached to it.

Everything impure lives here. The planner, the resolver and the renderer are pure
functions over data; deciding where the bytes go -- stdout, an archive file, a
socket, a browser -- is this module's job alone.
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..dedupe.models import DedupePlan, DuplicateGroup, snapshot_from_raw
from ..dedupe.planner import plan as build_plan
from ..dedupe.resolve import ApprovalPayload, Resolution, resolve_decisions
from ..report import APPROVE_PATH, ApprovalServer, render_plan_html
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

    # The archived copy is the record of what was proposed, so it is rendered without
    # the approval controls: an archive that could still submit something would be
    # lying about what it is.
    report_path = archive_report(config, render_plan_html(dedupe_plan))
    print(f"Report archived to {report_path}")

    resolution = review(dedupe_plan, port=args.port, open_browser=not args.no_browser)
    if resolution is None:
        return 130

    print()
    print(format_resolution(resolution))
    print()
    print("Nothing has been deleted: this run resolves decisions and stops there.")
    return 0


def review(dedupe_plan: DedupePlan, *, port: int, open_browser: bool) -> Resolution | None:
    """Serve the plan for review and block until it is approved, or abandoned.

    Returns the resolution, or None if the reviewer interrupted the run from the
    terminal. There is no third outcome and no timeout: closing the browser tab is
    not an answer, so the process simply keeps waiting for one.
    """
    html = render_plan_html(dedupe_plan, approve_url=APPROVE_PATH)

    def interpret(raw: Any) -> Resolution:
        # Runs on the request thread. Pure, and the only thing standing between a
        # submitted form and a deletion set -- so a payload that does not describe
        # this plan raises here, is answered as a 400 the reviewer can see, and ends
        # nothing.
        return resolve_decisions(dedupe_plan, ApprovalPayload.from_raw(raw))

    with ApprovalServer(html, interpret=interpret, port=port) as server:
        print()
        print(f"Review the plan at {server.url}")
        if open_browser:
            webbrowser.open(server.url)
            print("Opened it in your browser.")
        else:
            print("Not opening a browser (--no-browser).")
        print(
            "Waiting for your decision. Closing the tab does not cancel the run; "
            "press Ctrl-C here to abandon it."
        )
        try:
            return server.wait_for_decision()
        except KeyboardInterrupt:
            print()
            print("Run abandoned. Nothing in your library was changed.")
            return None


def format_resolution(resolution: Resolution) -> str:
    """The resolved deletion set, said plainly, with nothing summarised away."""
    lines = [
        "Resolved decisions",
        f"  albums to remove   {len(resolution.to_delete)}",
        f"  albums kept        {len(resolution.kept)}",
        f"  groups skipped     {len(resolution.skipped_groups)}",
    ]
    if not resolution.to_delete:
        lines.append("")
        lines.append("Nothing was selected for removal.")
        return "\n".join(lines)
    width = max(len(album.name) for album in resolution.restore_albums)
    lines.append("")
    lines.append("Would remove:")
    lines.extend(
        f"  {album.id}  {album.name:<{width}}  {album.artists}"
        for album in resolution.restore_albums
    )
    return "\n".join(lines)


def archive_report(config: Config, html: str) -> Path:
    """Write the rendered report to a timestamped file and return its path.

    Every run keeps its own copy: the report is the record of what the tool proposed
    at a given moment, and a later ranking change should not rewrite history.
    """
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = config.reports_dir / f"dedupe-report-{stamp}.html"
    path.write_text(html, encoding="utf-8")
    return path


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
