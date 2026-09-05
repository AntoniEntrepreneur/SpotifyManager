"""The `dedupe` subcommand: plan, then approve, then apply.

The run loads the library (cache or API), computes the plan, prints it, archives a
read-only copy of the report, then serves an interactive copy on a loopback port and
blocks until the reviewer submits their decisions. Those decisions are resolved into
an exact set of album ids and carried out: a restore file is written first, the
albums are removed in batches, and the same page the reviewer submitted from becomes
a results view stating exactly what succeeded, what failed, and what was never
attempted. The results report is archived too, as the permanent record of a deletion
that actually happened.

Everything impure lives here. The planner, the resolver, the executor's arithmetic
and the renderer are functions over data; deciding where the bytes go -- stdout, an
archive file, a socket, a browser, Spotify -- is this module's job alone.
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..dedupe.execute import Applied, execute, restore_command_for
from ..dedupe.models import DedupePlan, DuplicateGroup, snapshot_from_raw
from ..dedupe.planner import plan as build_plan
from ..dedupe.resolve import ApprovalPayload, resolve_decisions
from ..report import APPROVE_PATH, ApprovalServer, render_plan_html, render_results_html
from .library import build_client, load_library


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

    applied = review(
        dedupe_plan,
        config=config,
        port=args.port,
        open_browser=not args.no_browser,
        verbose=args.verbose,
    )
    if applied is None:
        return 130

    # Rendered a second time rather than carried back from the request thread: the
    # renderer is pure, so this is the same document byte for byte, and the archive
    # cannot drift from what the reviewer was shown.
    results_html = render_results_html(
        applied.result,
        applied.resolution,
        restore_command=restore_command_for(applied.result.restore_path),
    )
    archived = archive_report(config, results_html, name="dedupe-results")

    print()
    print(format_results(applied))
    print()
    print(f"Results archived to {archived}")
    # A run that did not remove everything it was approved to remove did not succeed,
    # and should not tell a shell script that it did.
    return 0 if applied.result.is_clean else 1


def review(
    dedupe_plan: DedupePlan,
    *,
    config: Config,
    port: int,
    open_browser: bool,
    verbose: bool,
) -> Applied | None:
    """Serve the plan for review, and carry out whatever comes back.

    Returns what was approved and what happened to it, or None if the reviewer
    interrupted the run from the terminal before approving anything. There is no
    third outcome and no timeout: closing the browser tab is not an answer, so the
    process simply keeps waiting for one.

    The apply runs on the request thread, inside the POST that submitted it. That is
    deliberate: it means the response can hand the browser a results page that is
    already rendered and already true, so the reviewer lands on the outcome in the
    tab they submitted from rather than on a promise that one is coming.
    """
    html = render_plan_html(dedupe_plan, approve_url=APPROVE_PATH)

    def interpret(raw: Any) -> Applied:
        # Runs on the request thread. `resolve_decisions` is pure and is the only
        # thing standing between a submitted form and a deletion set -- so a payload
        # that does not describe this plan raises here, is answered as a 400 the
        # reviewer can see, and ends nothing. Only past that line does anything
        # irreversible begin, and the restore file is written before it does.
        resolution = resolve_decisions(dedupe_plan, ApprovalPayload.from_raw(raw))
        result = execute(
            resolution,
            build_client(config, verbose=verbose),
            restores_dir=config.restores_dir,
            on_progress=lambda line: print(line, flush=True),
        )
        return Applied(resolution=resolution, result=result)

    def results_page(applied: Applied) -> str:
        return render_results_html(
            applied.result,
            applied.resolution,
            restore_command=restore_command_for(applied.result.restore_path),
        )

    with ApprovalServer(
        html, interpret=interpret, render_results=results_page, port=port
    ) as server:
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


def format_results(applied: Applied) -> str:
    """What the run did, in the terminal, with nothing summarised away.

    The same three classifications as the results page, named the same way. A user
    who reads only one of the two must not come away with a different picture.
    """
    result = applied.result
    resolution = applied.resolution
    lines = [
        "Results",
        f"  approved for removal   {result.requested_count}",
        f"  removed                {result.removed_count}",
        f"  failed                 {result.failed_count}",
        f"  never attempted        {result.never_attempted_count}",
        f"  kept                   {len(resolution.kept)}",
        f"  groups skipped         {len(resolution.skipped_groups)}",
    ]
    if result.restore_path is not None:
        lines += [
            "",
            f"Restore file: {result.restore_path}",
            f"Undo this run with:  {restore_command_for(result.restore_path)}",
        ]
    if result.nothing_requested:
        lines += ["", "You approved no removals. Nothing was deleted."]
        return "\n".join(lines)
    if result.is_clean:
        lines += ["", "Every album you approved was removed."]
        return "\n".join(lines)

    names = {album.id: album for album in resolution.restore_albums}

    def listing(title: str, ids: tuple[str, ...], note: str) -> list[str]:
        if not ids:
            return []
        out = ["", f"{title} ({len(ids)}) -- {note}"]
        out += [
            f"  {album_id}  {names[album_id].name if album_id in names else ''}"
            for album_id in ids
        ]
        return out

    lines += ["", "THIS RUN DID NOT FINISH. Your library is not what the plan described."]
    lines += listing(
        "Failed",
        result.failed_ids,
        "these requests were sent and errored; those albums may or may not still be saved",
    )
    lines += listing(
        "Never attempted",
        result.never_attempted_ids,
        "no request was ever issued for these; they are still saved",
    )
    for batch in result.failed_batches:
        lines += ["", f"Batch {batch.number} error: {batch.error}"]
    return "\n".join(lines)


def archive_report(config: Config, html: str, *, name: str = "dedupe-report") -> Path:
    """Write the rendered report to a timestamped file and return its path.

    Every run keeps its own copy: the report is the record of what the tool proposed
    at a given moment, and a later ranking change should not rewrite history.
    """
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = config.reports_dir / f"{name}-{stamp}.html"
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
