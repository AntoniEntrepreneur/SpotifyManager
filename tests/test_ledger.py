"""Not-duplicates ledger tests.

Two things are under test here. The first is the file itself: an unordered pair has
one representation, appending merges rather than replaces, and no damaged or missing
file ever raises.

The second is the reason the ledger stores *pairs* at all. A skipped group is
recorded as its member pairs, so the group never comes back -- but saving a new
edition of the same record later must surface the comparisons involving that new
album, and only those. That scenario is driven end to end from the real fixture:
plan, skip, record, re-plan, add an album, re-plan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotify_manager.dedupe import ledger
from spotify_manager.dedupe.models import (
    Artist,
    LibrarySnapshot,
    SavedAlbum,
    snapshot_from_raw,
)
from spotify_manager.dedupe.planner import plan
from spotify_manager.dedupe.resolve import (
    ApprovalPayload,
    GroupDecision,
    RESOLVE,
    SKIP,
    resolve_decisions,
)

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"

# The real Donda group: a plain edition, a Deluxe, and a second plain pressing.
DONDA_IDS = {
    "2Wiyo7LzdeBCsVZiRA6vVZ",
    "5CnpZV3q5BcESefcB3WJmz",
    "340MjPcVdiQRnMigrPybZA",
}
KANYE = "5K4W6rqBFWDnAN6FQUkS6x"


@pytest.fixture(scope="module")
def library() -> LibrarySnapshot:
    return snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return ledger.ledger_path(tmp_path)


def pair(a: str, b: str) -> frozenset[str]:
    return frozenset({a, b})


def group_of(dedupe_plan, album_id: str):
    for group in dedupe_plan.groups:
        if any(member.album.id == album_id for member in group.members):
            return group
    return None


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------


def test_a_missing_ledger_is_no_decisions_and_no_complaint(path):
    loaded = ledger.load(path)
    assert loaded.pairs == frozenset()
    assert loaded.warning is None
    assert not path.exists()


def test_recorded_pairs_survive_a_round_trip(path):
    ledger.record(path, [pair("a", "b"), pair("c", "d")])
    assert ledger.load(path).pairs == {pair("a", "b"), pair("c", "d")}


def test_a_pair_is_unordered_however_it_arrives(path):
    ledger.record(path, [pair("b", "a")])
    ledger.record(path, [pair("a", "b")])
    assert ledger.load(path).pairs == {pair("a", "b")}
    assert json.loads(path.read_text())["pairs"] == [["a", "b"]]


def test_the_file_stores_each_pair_sorted_so_it_cannot_be_written_twice(path):
    ledger.record(path, [pair("zz", "aa")])
    assert json.loads(path.read_text()) == {"version": 1, "pairs": [["aa", "zz"]]}


def test_recording_merges_with_what_is_already_there(path):
    ledger.record(path, [pair("a", "b")])
    added = ledger.record(path, [pair("c", "d")])
    assert added == 1
    assert ledger.load(path).pairs == {pair("a", "b"), pair("c", "d")}


def test_recording_a_pair_already_recorded_adds_nothing(path):
    ledger.record(path, [pair("a", "b")])
    assert ledger.record(path, [pair("b", "a")]) == 0
    assert len(ledger.load(path)) == 1


def test_recording_nothing_leaves_no_file_behind(path):
    assert ledger.record(path, ()) == 0
    assert not path.exists()


def test_clearing_reports_how_much_it_discarded_and_empties_the_ledger(path):
    ledger.record(path, [pair("a", "b"), pair("c", "d")])
    assert ledger.clear(path) == 2
    assert ledger.load(path).pairs == frozenset()
    assert ledger.load(path).warning is None


def test_clearing_an_empty_ledger_is_harmless(path):
    assert ledger.clear(path) == 0
    assert ledger.load(path).pairs == frozenset()


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "",
        "[]",
        '{"pairs": [["a", "b"]]}',  # no version
        '{"version": 99, "pairs": []}',  # a version we do not understand
        '{"version": 1}',  # no pairs
        '{"version": 1, "pairs": "a,b"}',
        '{"version": 1, "pairs": [["a"]]}',  # not a comparison
        '{"version": 1, "pairs": [["a", "a"]]}',  # not two distinct albums
        '{"version": 1, "pairs": [[1, 2]]}',
    ],
)
def test_a_corrupt_ledger_warns_instead_of_raising(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    loaded = ledger.load(path)
    assert loaded.pairs == frozenset()
    assert loaded.warning is not None
    assert str(path) in loaded.warning


def test_recording_over_a_corrupt_ledger_replaces_it(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated", encoding="utf-8")
    ledger.record(path, [pair("a", "b")])
    assert ledger.load(path) == ledger.LoadedLedger(pairs=frozenset({pair("a", "b")}))


def test_recording_something_that_is_not_a_pair_is_a_programming_error(path):
    with pytest.raises(ValueError):
        ledger.record(path, [frozenset({"a"})])


def test_the_ledger_lives_in_the_state_directory(tmp_path):
    assert ledger.ledger_path(tmp_path).parent == tmp_path
    assert ledger.ledger_path(tmp_path).suffix == ".json"


# --------------------------------------------------------------------------
# Skipping a real group, and what happens on the next run
# --------------------------------------------------------------------------


def skip_group_containing(dedupe_plan, album_id: str) -> ApprovalPayload:
    """The payload a reviewer submits by skipping the group holding `album_id`.

    Every other group keeps all its members, so this run deletes nothing and asserts
    nothing about any group but the one skipped -- exactly one judgement is made.
    """
    decisions: dict[int, GroupDecision] = {}
    for index, group in enumerate(dedupe_plan.groups):
        if any(m.album.id == album_id for m in group.members):
            decisions[index] = GroupDecision(action=SKIP)
        else:
            decisions[index] = GroupDecision(
                action=RESOLVE, keep=tuple(m.album.id for m in group.members)
            )
    return ApprovalPayload(groups=decisions)


def test_skipping_a_real_group_records_its_pairs_and_hides_it_next_run(library, path):
    first = plan(library)
    assert {m.album.id for m in group_of(first, "2Wiyo7LzdeBCsVZiRA6vVZ").members} == DONDA_IDS

    resolution = resolve_decisions(first, skip_group_containing(first, "2Wiyo7LzdeBCsVZiRA6vVZ"))
    assert ledger.record(path, resolution.asserted_distinct_pairs) == 3
    assert resolution.to_delete == ()

    # A separate run: nothing in memory carries over, only the file.
    second = plan(library, asserted_distinct=ledger.load(path).pairs)
    assert group_of(second, "2Wiyo7LzdeBCsVZiRA6vVZ") is None
    assert second.suppressed_group_count == 1
    assert len(second.groups) == len(first.groups) - 1


def test_clearing_decisions_brings_the_suppressed_group_back(library, path):
    first = plan(library)
    resolution = resolve_decisions(first, skip_group_containing(first, "2Wiyo7LzdeBCsVZiRA6vVZ"))
    ledger.record(path, resolution.asserted_distinct_pairs)
    assert ledger.clear(path) == 3

    restored = plan(library, asserted_distinct=ledger.load(path).pairs)
    assert group_of(restored, "2Wiyo7LzdeBCsVZiRA6vVZ") is not None
    assert restored.suppressed_group_count == 0
    assert len(restored.groups) == len(first.groups)


def test_a_new_edition_of_a_skipped_group_surfaces_only_the_unjudged_comparisons(
    library, path
):
    """The reason the ledger stores pairs rather than groups or keys.

    Donda is skipped, so its three comparisons are settled and the group vanishes.
    Then a fourth edition is saved. The group comes back -- because three brand new
    comparisons exist that nobody has judged -- and it comes back carrying exactly
    the three old judgements, so the user is asked about the new album and nothing
    else.
    """
    first = plan(library)
    resolution = resolve_decisions(first, skip_group_containing(first, "2Wiyo7LzdeBCsVZiRA6vVZ"))
    ledger.record(path, resolution.asserted_distinct_pairs)
    judged = ledger.load(path).pairs
    assert group_of(plan(library, asserted_distinct=judged), "5CnpZV3q5BcESefcB3WJmz") is None

    # The user saves a fourth Donda edition.
    newly_saved = SavedAlbum(
        id="new-donda-edition",
        name="Donda (Super Deluxe Edition)",
        artists=(Artist(id=KANYE, name="Kanye West"),),
        album_type="album",
        release_date="2022-01-01",
        release_date_precision="day",
        total_tracks=40,
    )
    grown = LibrarySnapshot(
        fetched_at=library.fetched_at, albums=library.albums + (newly_saved,)
    )

    result = plan(grown, asserted_distinct=judged)
    group = group_of(result, "new-donda-edition")
    assert group is not None, "a new album's comparisons have never been judged"
    assert result.suppressed_group_count == 0
    assert {m.album.id for m in group.members} == DONDA_IDS | {"new-donda-edition"}

    # Three of the six comparisons are settled; the three the new album brings are
    # exactly the ones still open.
    assert group.suppressed_pair_count == 3
    all_pairs = {
        frozenset({a, b})
        for a in DONDA_IDS | {"new-donda-edition"}
        for b in DONDA_IDS | {"new-donda-edition"}
        if a < b
    }
    assert all_pairs - judged == {
        frozenset({"new-donda-edition", album_id}) for album_id in DONDA_IDS
    }


def test_skipping_the_grown_group_again_records_only_the_new_pairs(library, path):
    """A second skip appends; it never re-writes a judgement already stored."""
    first = plan(library)
    ledger.record(
        path,
        resolve_decisions(
            first, skip_group_containing(first, "2Wiyo7LzdeBCsVZiRA6vVZ")
        ).asserted_distinct_pairs,
    )

    newly_saved = SavedAlbum(
        id="new-donda-edition",
        name="Donda (Deluxe Edition)",
        artists=(Artist(id=KANYE, name="Kanye West"),),
        album_type="album",
        release_date="2022-01-01",
        release_date_precision="day",
        total_tracks=40,
    )
    grown = LibrarySnapshot(
        fetched_at=library.fetched_at, albums=library.albums + (newly_saved,)
    )
    second = plan(grown, asserted_distinct=ledger.load(path).pairs)
    resolution = resolve_decisions(second, skip_group_containing(second, "new-donda-edition"))

    assert ledger.record(path, resolution.asserted_distinct_pairs) == 3
    assert len(ledger.load(path)) == 6
    assert group_of(plan(grown, asserted_distinct=ledger.load(path).pairs), "new-donda-edition") is None
