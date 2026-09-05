"""The like planner, driven by the redacted snapshot of the real library.

This is the seam where every rule this feature has is decided, so it is where those
rules are tested: what counts as the same recording, which copy of one survives, what
is already liked, what cannot be liked at all, and what order the rest go out in.
Nothing here reaches a network, a filesystem or a clock, because the planner cannot.

Claims are about outcomes -- which track ids come out, and which do not. Small
hand-built libraries are used for the cases the real fixture cannot show (a local
file, a compilation competing with an album, an exact tie); the fixture drives the
claims that are only meaningful at real scale.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from spotify_manager.dedupe.models import Artist, SavedAlbum
from spotify_manager.likes.models import (
    AlbumTracks,
    Track,
    TrackLibrary,
    liked_ids_from_raw,
    library_from_raw,
)
from spotify_manager.likes.planner import format_plan, plan_likes

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"
LIKES_PACKAGE = Path(__file__).parents[1] / "src" / "spotify_manager" / "likes"


@pytest.fixture(scope="module")
def library() -> TrackLibrary:
    return library_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def real_plan(library):
    return plan_likes(library, frozenset())


# -- purity ------------------------------------------------------------------

#: The two modules in this package that are deliberately not pure, named one by one
#: so that adding a third to a package whose planning path must cost nothing has to
#: be an explicit decision. `record.py` owns the run record; `execute.py` writes it
#: and issues the likes, which needs a clock, a path and the API.
IO_OWNING_MODULES = ("record.py", "execute.py")
FORBIDDEN_IMPORTS = ("infra", "requests", "spotipy", "urllib", "pathlib", "time", "datetime")


def _imports_of(module: str) -> set[str]:
    tree = ast.parse((LIKES_PACKAGE / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    return imported


PURE_MODULES = sorted(
    p.name for p in LIKES_PACKAGE.glob("*.py") if p.name not in IO_OWNING_MODULES
)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_planner_cannot_reach_the_io_shell(module):
    for name in _imports_of(module):
        assert name.split(".")[0] not in FORBIDDEN_IMPORTS, f"{module} imports {name!r}"


def test_only_the_named_modules_in_the_package_own_a_file():
    """A new I/O-owning module must be a deliberate decision, not a slow drift."""
    io_owning = {
        p.name
        for p in LIKES_PACKAGE.glob("*.py")
        if {"pathlib", "json", "os"} & {n.split(".")[0] for n in _imports_of(p.name)}
    }
    assert io_owning == set(IO_OWNING_MODULES)


# -- small hand-built libraries ----------------------------------------------


def _album(album_id, name, *, album_type="album", total_tracks=10, release="2020-01-01",
           added_at="2020-01-01T00:00:00Z", artist="ar1"):
    return SavedAlbum(
        id=album_id,
        name=name,
        artists=(Artist(id=artist, name="The Band"),),
        album_type=album_type,
        release_date=release,
        release_date_precision="day",
        total_tracks=total_tracks,
        added_at=added_at,
    )


def _track(track_id, name="Song", *, duration_ms=200_000, disc=1, number=1, local=False,
           artists=("The Band",)):
    return Track(
        id=track_id,
        name=name,
        artist_names=artists,
        duration_ms=duration_ms,
        disc_number=disc,
        track_number=number,
        is_local=local,
    )


def _library(*entries):
    return TrackLibrary(fetched_at="test", albums=tuple(entries))


def test_every_track_on_every_album_is_liked_when_nothing_is_liked_yet():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("al1", "One"), (_track("t1"), _track("t2", "Other", number=2))),
            AlbumTracks(_album("al2", "Two", added_at="2021-01-01T00:00:00Z"), (_track("t3", "Third"),)),
        ),
        frozenset(),
    )
    assert plan.track_ids == ("t1", "t2", "t3")


def test_a_track_already_liked_is_not_liked_again():
    plan = plan_likes(
        _library(AlbumTracks(_album("al1", "One"), (_track("t1"), _track("t2", "Other", number=2)))),
        frozenset({"t1"}),
    )
    assert plan.track_ids == ("t2",)
    assert plan.already_liked_count == 1


def test_a_local_file_is_never_attempted_and_is_reported():
    plan = plan_likes(
        _library(AlbumTracks(_album("al1", "One"), (_track("t1", local=True), _track("t2", "B", number=2)))),
        frozenset(),
    )
    assert plan.track_ids == ("t2",)
    assert plan.unlikeable_count == 1
    assert "Cannot be liked: 1" in format_plan(plan)


def test_a_track_with_no_id_is_never_attempted():
    plan = plan_likes(_library(AlbumTracks(_album("al1", "One"), (_track(""),))), frozenset())
    assert plan.track_ids == ()
    assert plan.unlikeable_count == 1


def test_the_same_recording_on_two_editions_is_liked_once():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("plain", "Record"), (_track("t1"),)),
            AlbumTracks(_album("deluxe", "Record (Deluxe)"), (_track("t2"),)),
        ),
        frozenset(),
    )
    assert plan.track_ids == ("t2",)
    assert plan.collapsed_count == 1


def test_the_richer_edition_supplies_the_surviving_copy():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("remaster", "Record (Remastered)"), (_track("t1"),)),
            AlbumTracks(_album("deluxe", "Record (Deluxe)"), (_track("t2"),)),
        ),
        frozenset(),
    )
    assert plan.track_ids == ("t2",)


def test_a_compilation_never_wins_against_a_real_album():
    """Even a huge, recent compilation loses to the record the song came from."""
    plan = plan_likes(
        _library(
            AlbumTracks(_album("album", "Record", total_tracks=10, release="1999-01-01"), (_track("t1"),)),
            AlbumTracks(
                _album("hits", "Greatest Hits", album_type="compilation", total_tracks=40, release="2015-01-01"),
                (_track("t2"),),
            ),
        ),
        frozenset(),
    )
    assert plan.track_ids == ("t1",)


def test_two_compilations_still_resolve_to_exactly_one():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("c1", "Hits", album_type="compilation"), (_track("t1"),)),
            AlbumTracks(_album("c2", "More Hits", album_type="compilation"), (_track("t2"),)),
        ),
        frozenset(),
    )
    assert len(plan.track_ids) == 1


def test_a_recording_already_liked_suppresses_its_other_edition():
    """The song is in Liked Songs already; the other copy is a duplicate, not a gap."""
    plan = plan_likes(
        _library(
            AlbumTracks(_album("plain", "Record"), (_track("t1"),)),
            AlbumTracks(_album("deluxe", "Record (Deluxe)"), (_track("t2"),)),
        ),
        frozenset({"t1"}),
    )
    assert plan.track_ids == ()
    assert plan.already_liked_count == 1
    assert plan.collapsed_count == 1


def test_a_second_apart_is_the_same_recording_and_ten_seconds_is_not():
    same = plan_likes(
        _library(
            AlbumTracks(_album("a1", "One"), (_track("t1", duration_ms=200_000),)),
            AlbumTracks(_album("a2", "Two"), (_track("t2", duration_ms=200_400),)),
        ),
        frozenset(),
    )
    assert len(same.track_ids) == 1

    different = plan_likes(
        _library(
            AlbumTracks(_album("a1", "One"), (_track("t1", duration_ms=200_000),)),
            AlbumTracks(_album("a2", "Two"), (_track("t2", duration_ms=210_000),)),
        ),
        frozenset(),
    )
    assert len(different.track_ids) == 2


def test_the_same_title_by_a_different_artist_is_not_the_same_recording():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("a1", "One"), (_track("t1", artists=("The Band",)),)),
            AlbumTracks(_album("a2", "Two"), (_track("t2", artists=("Someone Else",)),)),
        ),
        frozenset(),
    )
    assert set(plan.track_ids) == {"t1", "t2"}


def test_a_tie_between_identical_editions_is_broken_the_same_way_every_time():
    entries = (
        AlbumTracks(_album("bbb", "Record"), (_track("t2"),)),
        AlbumTracks(_album("aaa", "Record"), (_track("t1"),)),
    )
    first = plan_likes(_library(*entries), frozenset())
    second = plan_likes(_library(*reversed(entries)), frozenset())
    assert first.track_ids == second.track_ids
    assert len(first.track_ids) == 1


def test_likes_go_out_oldest_saved_album_first():
    plan = plan_likes(
        _library(
            AlbumTracks(_album("new", "New", added_at="2024-01-01T00:00:00Z"), (_track("t_new", "N"),)),
            AlbumTracks(_album("old", "Old", added_at="2011-01-01T00:00:00Z"), (_track("t_old", "O"),)),
        ),
        frozenset(),
    )
    assert plan.track_ids == ("t_old", "t_new")


def test_within_an_album_likes_go_out_in_disc_and_track_order():
    plan = plan_likes(
        _library(
            AlbumTracks(
                _album("al1", "One"),
                (
                    _track("d2t1", "D", disc=2, number=1),
                    _track("d1t2", "B", disc=1, number=2),
                    _track("d1t1", "A", disc=1, number=1),
                ),
            )
        ),
        frozenset(),
    )
    assert plan.track_ids == ("d1t1", "d1t2", "d2t1")


def test_a_truncated_album_is_reported_as_incomplete():
    plan = plan_likes(
        _library(AlbumTracks(_album("al1", "Box Set"), (_track("t1"),), truncated=True)),
        frozenset(),
    )
    assert plan.incomplete_album_ids == ("al1",)
    assert "Track list incomplete for 1 album(s)" in format_plan(plan)


def test_an_empty_library_plans_nothing_and_says_so():
    plan = plan_likes(_library(), frozenset())
    assert plan.nothing_to_do
    assert "Nothing to do." in format_plan(plan)


# -- the real library --------------------------------------------------------


def test_every_track_found_is_accounted_for_exactly_once(real_plan):
    """The counts are a partition of the library, not a summary of it."""
    assert real_plan.accounted_for == real_plan.tracks_found


def test_the_real_library_collapses_its_duplicate_recordings(real_plan):
    assert real_plan.tracks_found == 10710
    assert real_plan.collapsed_count == 1237
    assert real_plan.to_like_count == 9473


def test_no_track_id_is_ever_liked_twice(real_plan):
    assert len(set(real_plan.track_ids)) == len(real_plan.track_ids)


def test_planning_the_real_library_is_deterministic(library):
    assert plan_likes(library, frozenset()).track_ids == plan_likes(library, frozenset()).track_ids


def test_liking_the_whole_library_then_replanning_finds_nothing_left(library, real_plan):
    """Re-running after a clean run is a no-op, which is what makes a re-run a resume."""
    again = plan_likes(library, frozenset(real_plan.track_ids))
    assert again.nothing_to_do


def test_a_half_finished_run_replans_to_exactly_the_remainder(library, real_plan):
    done = real_plan.track_ids[: len(real_plan.track_ids) // 2]
    again = plan_likes(library, frozenset(done))
    assert again.track_ids == real_plan.track_ids[len(done) :]


def test_the_two_truncated_albums_in_the_real_library_are_named(real_plan):
    assert len(real_plan.incomplete_album_ids) == 2


def test_liked_ids_are_read_from_a_liked_songs_snapshot():
    raw = {
        "fetched_at": "now",
        "tracks": [
            {"added_at": "x", "track": {"id": "t1"}},
            {"added_at": "x", "track": {"id": "t2"}},
            {"added_at": "x", "track": {"id": None}},
            {"added_at": "x", "track": None},
            "not an item",
        ],
    }
    assert liked_ids_from_raw(raw) == frozenset({"t1", "t2"})


def test_an_empty_liked_songs_snapshot_is_no_liked_ids():
    assert liked_ids_from_raw({"fetched_at": "now", "tracks": []}) == frozenset()
