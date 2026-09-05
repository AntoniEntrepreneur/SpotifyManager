"""The rendered report, asserted only on what a reader (or a browser) can observe.

The report is presentation, so the claims worth pinning are the ones a change could
plausibly break without anyone noticing: the document depends on nothing external,
its headline numbers are the plan's own numbers, every group is actually in the
document, and text that came from an album title cannot escape into markup.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from spotify_manager.dedupe.execute import BatchOutcome, ExecutionResult
from spotify_manager.dedupe.models import (
    AlbumJudgement,
    Artist,
    DedupePlan,
    DuplicateGroup,
    GroupKey,
    Image,
    SavedAlbum,
    snapshot_from_raw,
)
from spotify_manager.dedupe.planner import plan
from spotify_manager.dedupe.resolve import Resolution, RestoreAlbum, SkippedGroup
from spotify_manager.report import render_plan_html, render_results_html

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"
RENDER_MODULE = Path(__file__).parents[1] / "src" / "spotify_manager" / "report" / "render.py"

#: A title carrying every character that breaks a naive renderer, plus a `$` because
#: `$` is what breaks a naive *templating* one.
HOSTILE_TITLE = 'Rock & Roll <script>alert("x")</script> $5 — "Deluxe" & <b>bold</b>'


@pytest.fixture(scope="module")
def real_plan() -> DedupePlan:
    return plan(snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8"))))


@pytest.fixture(scope="module")
def real_html(real_plan: DedupePlan) -> str:
    return render_plan_html(real_plan)


def _album(album_id: str, name: str, tracks: int = 10) -> SavedAlbum:
    return SavedAlbum(
        id=album_id,
        name=name,
        artists=(Artist(id="artist-1", name='Sam & "The" <Band>'),),
        album_type="album",
        release_date="2019-03-04",
        release_date_precision="day",
        total_tracks=tracks,
        images=(Image(url="https://i.scdn.co/image/abc", height=300, width=300),),
    )


def _plan_with(*names: str, suppressed_groups: int = 0) -> DedupePlan:
    albums = [_album(f"id{index}", name) for index, name in enumerate(names)]
    members = tuple(
        AlbumJudgement(
            album=album,
            edition_rank=80 if index == 0 else 0,
            edition_category="deluxe" if index == 0 else "plain",
            ignored_decorations=("Deluxe & <Expanded>",) if index == 0 else (),
            is_keeper=index == 0,
        )
        for index, album in enumerate(albums)
    )
    group = DuplicateGroup(
        key=GroupKey(normalized_title="rock & roll", primary_artist_id="artist-1", album_type="album"),
        members=members,
        keeper_id=albums[0].id,
        ignored_decorations=("Deluxe & <Expanded>",),
    )
    return DedupePlan(
        groups=(group,),
        total_albums_scanned=len(albums) + 7,
        excluded_reasons={"compilation": 24, "unreadable & odd": 2},
        suppressed_group_count=suppressed_groups,
    )


def test_the_document_depends_on_nothing_but_spotify_cover_art(real_html: str):
    assert "<script src" not in real_html
    assert "<link " not in real_html
    assert "@import" not in real_html
    hosts = set(re.findall(r"https?://([^/\"'\s]+)", real_html))
    assert hosts == {"i.scdn.co"}, hosts
    assert "i.scdn.co" in real_html.split("<footer>")[1]  # the trade-off is stated


def test_the_header_states_the_plans_own_numbers(real_plan: DedupePlan, real_html: str):
    header = real_html.split("</header>")[0]
    for value, label in (
        (real_plan.total_albums_scanned, "albums scanned"),
        (len(real_plan.groups), "duplicate groups"),
        (real_plan.proposed_removal_count, "proposed for removal"),
        (real_plan.suppressed_group_count, "suppressed groups"),
    ):
        assert f'<span class="n">{value}</span><span class="l">{label}</span>' in header


def test_the_suppressed_stat_reads_the_plan_rather_than_a_constant():
    html = render_plan_html(_plan_with("A (Deluxe)", "A", suppressed_groups=3))
    assert '<span class="n">3</span><span class="l">suppressed groups</span>' in html
    assert "<strong>3</strong> groups were suppressed" in html


def test_every_group_and_every_album_is_in_the_document(real_plan: DedupePlan, real_html: str):
    assert real_html.count('<section class="group"') == len(real_plan.groups)
    for index, group in enumerate(real_plan.groups):
        assert f'id="group-{index}"' in real_html
        assert f'data-size="{len(group.members)}"' in real_html
        for member in group.members:
            assert f'data-album-id="{member.album.id}"' in real_html
    keepers = real_html.count('data-keeper="true"')
    assert keepers == len(real_plan.groups)
    assert real_html.count('data-keeper="false"') == real_plan.proposed_removal_count


def test_the_keeper_is_marked_and_the_rest_are_not(real_plan: DedupePlan, real_html: str):
    assert real_html.count('class="badge keep"') == len(real_plan.groups)
    assert real_html.count('class="badge remove"') == real_plan.proposed_removal_count
    assert real_html.count('class="album keeper"') == len(real_plan.groups)


def test_each_album_shows_cover_art_title_artist_year_tracks_and_rank(real_plan: DedupePlan, real_html: str):
    group = real_plan.groups[0]
    card = real_html.split(f'data-album-id="{group.keeper.album.id}"')[1].split("</article>")[0]
    assert 'class="cover" src="https://i.scdn.co/image/' in card
    assert group.keeper.album.name in card
    assert group.keeper.album.artist_names in card
    assert str(group.keeper.album.release_sort_key[0]) in card
    assert f"{group.keeper.album.total_tracks} tracks" in card
    assert f"rank {group.keeper.edition_rank}" in card


def test_each_group_shows_its_key_and_the_decorations_it_ignored(real_plan: DedupePlan, real_html: str):
    for group in real_plan.groups:
        assert f'class="key mono">{group.key.normalized_title}<' in real_html
    donda = [g for g in real_plan.groups if g.key.normalized_title == "donda"][0]
    card = real_html.split(f'id="group-{real_plan.groups.index(donda)}"')[1].split("</section>")[0]
    assert '<span class="chip">Deluxe</span>' in card


def test_a_title_full_of_markup_is_escaped_rather_than_rendered():
    html = render_plan_html(_plan_with(HOSTILE_TITLE, "Rock & Roll"))

    assert HOSTILE_TITLE not in html
    assert "<script>alert" not in html
    assert "<b>bold</b>" not in html
    assert "Rock &amp; Roll &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; $5" in html
    assert "Sam &amp; &quot;The&quot; &lt;Band&gt;" in html
    assert "Deluxe &amp; &lt;Expanded&gt;" in html
    # The searchable attribute is derived from the same text and must be safe too.
    assert 'data-search="rock &amp; roll' in html


def test_the_footer_reports_every_exclusion_reason_it_was_given():
    html = render_plan_html(_plan_with("A (Deluxe)", "A"))
    footer = html.split("<footer>")[1]
    assert "<strong>26</strong> albums were excluded" in footer
    assert "24 compilation" in footer
    assert "2 unreadable &amp; odd" in footer


def test_an_empty_plan_still_renders_a_document():
    html = render_plan_html(DedupePlan(total_albums_scanned=12))
    assert "No duplicate editions found." in html
    assert "No albums were excluded from analysis." in html
    assert html.strip().endswith("</html>")


def test_rendering_cannot_reach_the_filesystem_the_network_or_the_clock():
    """Re-rendering must stay free; the cheapest guarantee is that it cannot do I/O."""
    tree = ast.parse(RENDER_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("pathlib", "os", "time", "datetime", "requests", "urllib", "webbrowser")
    for name in imported:
        assert name.split(".")[0] not in forbidden, f"render.py imports {name!r}"


# --------------------------------------------------------------------------
# The approval mode: the same document, plus the controls that submit it
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def approving_html(real_plan: DedupePlan) -> str:
    return render_plan_html(real_plan, approve_url="/approve")


def test_the_archived_report_has_no_way_to_submit_anything(real_html: str):
    """An archive is a record. A record that could still approve a deletion is a lie."""
    assert "keep-box" not in real_html
    assert "skip-box" not in real_html
    assert "fetch(" not in real_html
    assert "/approve" not in real_html


def test_every_album_offers_a_keep_checkbox(real_plan: DedupePlan, approving_html: str):
    assert approving_html.count('class="keep-box"') == real_plan.albums_in_groups
    for group in real_plan.groups:
        for member in group.members:
            assert f'data-album-id="{member.album.id}" data-proposed-keep=' in approving_html


def test_the_boxes_start_ticked_exactly_as_the_plan_proposes(
    real_plan: DedupePlan, approving_html: str
):
    """Submitting without touching anything must reproduce the plan, so the ticks
    must start as the plan's own proposal -- one keeper per group, nothing else."""
    assert approving_html.count('data-proposed-keep="true" checked') == len(real_plan.groups)
    assert approving_html.count('data-proposed-keep="false">') == real_plan.proposed_removal_count
    assert 'data-proposed-keep="false" checked' not in approving_html


def test_every_group_offers_a_skip_control(real_plan: DedupePlan, approving_html: str):
    assert approving_html.count('class="skip-box"') == len(real_plan.groups)
    for index in range(len(real_plan.groups)):
        assert f'class="skip-box" data-group-index="{index}"' in approving_html


def test_one_button_approves_the_whole_reviewed_plan(approving_html: str):
    assert approving_html.count('id="approve"') == 1
    assert 'id="approve-bar"' in approving_html


def test_the_page_only_ever_posts_back_to_whatever_served_it(approving_html: str):
    """A path, never an origin: the decisions cannot be aimed anywhere else."""
    assert 'var APPROVE_URL = "/approve";' in approving_html
    hosts = set(re.findall(r"https?://([^/\"'\s]+)", approving_html))
    assert hosts == {"i.scdn.co"}, hosts
    assert "<script src" not in approving_html
    assert "<link " not in approving_html


def test_the_reviewer_is_told_that_closing_the_tab_does_not_cancel(approving_html: str):
    assert "Closing this tab does not cancel the run" in approving_html


def test_an_empty_plan_still_renders_an_approvable_document():
    html = render_plan_html(DedupePlan(total_albums_scanned=12), approve_url="/approve")
    assert "No duplicate editions found." in html
    assert 'id="approve"' in html
    assert html.strip().endswith("</html>")


# --------------------------------------------------------------------------
# The results view: what actually happened, after decisions were carried out
# --------------------------------------------------------------------------

RESTORE_COMMAND = "spotify-manager restore /tmp/restores/restore-2026-09-03T14-30-00.json"


def _restore_album(index: int) -> RestoreAlbum:
    return RestoreAlbum(
        id=f"alb{index:04d}",
        name=f"Album {index} (Deluxe Edition)",
        artists="Some Artist",
        release_date="2019-09-27",
        total_tracks=12,
    )


def _resolution(count: int, *, kept: int = 0, skipped_size: int = 0) -> Resolution:
    albums = tuple(_restore_album(i) for i in range(count))
    skipped = (
        (SkippedGroup(key=GroupKey(normalized_title="x", primary_artist_id="a", album_type="album"),
                      album_ids=tuple(f"skip{i}" for i in range(skipped_size))),)
        if skipped_size
        else ()
    )
    return Resolution(
        to_delete=tuple(a.id for a in albums),
        kept=tuple(f"keep{i}" for i in range(kept)),
        skipped_groups=skipped,
        restore_albums=albums,
    )


def test_a_clean_result_states_removed_count_and_no_failures():
    resolution = _resolution(3, kept=2)
    result = ExecutionResult(
        batches=(BatchOutcome(number=1, album_ids=resolution.to_delete, status="succeeded", attempts=1),),
        requested_ids=resolution.to_delete,
        restore_path=Path("/tmp/restores/restore-2026-09-03T14-30-00.json"),
        created_at="2026-09-03T14:30:00+00:00",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert '<span class="n">3</span><span class="l">removed</span>' in html
    assert '<span class="n">0</span><span class="l">failed</span>' in html
    assert '<span class="n">0</span><span class="l">never attempted</span>' in html
    assert "All 3 approved albums removed." in html
    # No failure or never-attempted panel is rendered at all for a clean run.
    assert "state unknown" not in html
    assert "never attempted &mdash; still saved" not in html
    assert html.strip().endswith("</html>")


def test_a_run_that_approved_nothing_still_renders_a_results_view():
    resolution = Resolution(kept=("a", "b"))
    result = ExecutionResult(requested_ids=(), created_at="2026-09-03T14:30:00+00:00")

    html = render_results_html(result, resolution, restore_command="")

    assert "Nothing was removed." in html
    assert "No restore file" in html
    assert '<span class="n">0</span><span class="l">approved for removal</span>' in html
    assert html.strip().endswith("</html>")


def test_a_partial_failure_states_every_count_exactly_and_never_summarises_it_away():
    resolution = _resolution(6)
    result = ExecutionResult(
        batches=(
            BatchOutcome(number=1, album_ids=resolution.to_delete[:2], status="succeeded", attempts=1),
            BatchOutcome(
                number=2,
                album_ids=resolution.to_delete[2:4],
                status="failed",
                attempts=4,
                error="RuntimeError: Spotify said no",
            ),
            BatchOutcome(number=3, album_ids=resolution.to_delete[4:], status="never_attempted"),
        ),
        requested_ids=resolution.to_delete,
        restore_path=Path("/tmp/restores/restore-2026-09-03T14-30-00.json"),
        created_at="2026-09-03T14:30:00+00:00",
        interrupted="KeyboardInterrupt",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert "did not finish" in html
    # The tab title and the banner both state the true, unfinished count.
    assert "INCOMPLETE" in html
    assert '<span class="n">2</span><span class="l">removed</span>' in html
    assert '<span class="n">2</span><span class="l">failed</span>' in html
    assert '<span class="n">2</span><span class="l">never attempted</span>' in html
    # Every failed and never-attempted album is actually named, not just counted.
    for album_id in resolution.to_delete[2:4]:
        assert album_id in html.split("state unknown")[1].split("still saved")[0]
    for album_id in resolution.to_delete[4:]:
        assert album_id in html
    assert "Spotify said no" in html
    assert "KeyboardInterrupt" in html
    assert html.strip().endswith("</html>")


def test_the_results_page_names_the_restore_file_and_the_undo_command():
    resolution = _resolution(2)
    result = ExecutionResult(
        batches=(BatchOutcome(number=1, album_ids=resolution.to_delete, status="succeeded", attempts=1),),
        requested_ids=resolution.to_delete,
        restore_path=Path("/tmp/restores/restore-2026-09-03T14-30-00.json"),
        created_at="2026-09-03T14:30:00+00:00",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert "/tmp/restores/restore-2026-09-03T14-30-00.json" in html
    assert RESTORE_COMMAND in html


def test_the_results_page_is_self_contained_except_cover_art():
    resolution = _resolution(2)
    result = ExecutionResult(
        batches=(BatchOutcome(number=1, album_ids=resolution.to_delete, status="succeeded", attempts=1),),
        requested_ids=resolution.to_delete,
        restore_path=Path("/tmp/restores/restore-2026-09-03T14-30-00.json"),
        created_at="2026-09-03T14:30:00+00:00",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert "<script src" not in html
    assert "<link " not in html
    assert "@import" not in html
    hosts = set(re.findall(r"https?://([^/\"'\s]+)", html))
    assert hosts in (set(), {"i.scdn.co"}), hosts


def test_the_results_page_carries_no_approval_controls():
    """A results page describes a decision already carried out; it must not be able
    to submit anything -- there is no plan left to submit it against."""
    resolution = _resolution(2)
    result = ExecutionResult(
        batches=(BatchOutcome(number=1, album_ids=resolution.to_delete, status="succeeded", attempts=1),),
        requested_ids=resolution.to_delete,
        restore_path=Path("/tmp/restores/restore-2026-09-03T14-30-00.json"),
        created_at="2026-09-03T14:30:00+00:00",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert "keep-box" not in html
    assert "skip-box" not in html
    assert "fetch(" not in html
    assert "/approve" not in html
    assert "<form" not in html
    assert "<button" not in html


def test_the_results_page_escapes_hostile_album_names():
    resolution = Resolution(
        to_delete=("hostile1",),
        restore_albums=(
            RestoreAlbum(
                id="hostile1",
                name=HOSTILE_TITLE,
                artists='Sam & "The" <Band>',
                release_date="2019-03-04",
                total_tracks=10,
            ),
        ),
    )
    result = ExecutionResult(
        batches=(BatchOutcome(number=1, album_ids=("hostile1",), status="failed", attempts=4, error="boom"),),
        requested_ids=("hostile1",),
        restore_path=Path("/tmp/restores/restore-x.json"),
        created_at="2026-09-03T14:30:00+00:00",
    )
    html = render_results_html(result, resolution, restore_command=RESTORE_COMMAND)

    assert HOSTILE_TITLE not in html
    assert "<script>alert" not in html
    assert "Sam &amp; &quot;The&quot; &lt;Band&gt;" in html


def test_rendering_results_cannot_reach_the_filesystem_the_network_or_the_clock():
    """Same purity guarantee as the plan renderer -- both live in this module."""
    tree = ast.parse(RENDER_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("pathlib", "os", "time", "datetime", "requests", "urllib", "webbrowser")
    for name in imported:
        assert name.split(".")[0] not in forbidden, f"render.py imports {name!r}"
