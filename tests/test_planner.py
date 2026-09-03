"""Planner tests, driven by the redacted snapshot of the real library.

These assert observable outcomes only: which real albums end up in a group together,
which one is proposed as the keeper, and what the plan reports. Nothing here pins a
normalized string or any other internal step -- the claim under test is "these two
albums are the same record and this is the one to keep", and that is what is
checked.

Album ids are the real ones from the fixture, so a rule change that breaks a
judgement names the album it broke.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotify_manager.dedupe.models import (
    Artist,
    LibrarySnapshot,
    SavedAlbum,
    snapshot_from_raw,
)
from spotify_manager.dedupe.planner import plan

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"


@pytest.fixture(scope="module")
def library() -> LibrarySnapshot:
    return snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def real_plan(library: LibrarySnapshot):
    return plan(library)


def group_of(dedupe_plan, album_id: str):
    """The group containing an album, or None if the planner left it alone."""
    for group in dedupe_plan.groups:
        if any(member.album.id == album_id for member in group.members):
            return group
    return None


def ids_grouped_together(dedupe_plan, album_id: str) -> set[str]:
    group = group_of(dedupe_plan, album_id)
    return {m.album.id for m in group.members} if group else set()


# --------------------------------------------------------------------------
# Albums that must group
# --------------------------------------------------------------------------

# (case, plain album id, decorated album id) -- all from the real library.
MUST_GROUP = [
    ("deluxe", "5CnpZV3q5BcESefcB3WJmz", "2Wiyo7LzdeBCsVZiRA6vVZ"),  # Donda
    ("deluxe edition", "47BiFcV59TQi2s9SkBo2pb", "1gUI4keDXbeSil6rwY9qUm"),  # Recovery
    ("deluxe in brackets", "7h2OEj0ifXb3UdgvTmCqfY", "6HXlnhMr40FP2qBTnKXRbA"),  # Relapse
    ("expanded edition", "78Fgb88MY0ECc4GVMejqTg", "13heDPCGwoIdufMoHIyjmh"),  # It Was Written
    ("complete edition", "7oc6i8xUILaWwQ5BJMZS3c", "0pFydyko4Iw450abXlDPpp"),  # Tha Carter IV
    ("remastered", "6Srtm8a14PDdrpRUdvUdEO", "1VW1MFNstaJuygaoTPkdCk"),  # Morning Glory?
    ("year remaster", "6uj0z3xtvQ8Y0UWevdQlIx", "5SBHID8qGG3x52zgoh2ilz"),  # Sheer Heart Attack
    ("nth anniversary", "2VBcztE58pBKjIDS5oEgFh", "3rYMmOfNQlWXYrbd8yXqJ1"),  # Acid Rap
    ("n year anniversary", "7viNUmZZ8ztn2UB4XB3jIL", "2fSAC0ZiYnwKfzLEvyaMm8"),  # 2014 FHD
    ("deluxe anniversary", "6ZOXiVL8rmk2ATHJiFJhiD", "22rKa9MG4cHIRxvL1Vbs0q"),  # Rolling Papers
    ("featured artists", "6XJQyWSjUaPb0M30hX2QU8", "3yqVwj5ze0hxUt28pFReRC"),  # BLOOD ON MY NIKEZ
    ("diacritics", "43uErencdmuTRFZPG3zXL1", "4Qvh33TMiFWZIOoSMcM4PM"),  # Piñata
    ("punctuation only", "0tS7jye0zKwGF0H4rqFGvz", "6ZS2NOEZ7XwlNlKWK2dzPM"),  # Fear of God II
    ("identical titles", "4CzT5ueFBRpbILw34HQYxi", "78iX7tMceN0FsnmabAtlOC"),  # All Eyez On Me
]


@pytest.mark.parametrize(("case", "left", "right"), MUST_GROUP, ids=[c[0] for c in MUST_GROUP])
def test_editions_of_one_record_group_together(real_plan, case, left, right):
    assert right in ids_grouped_together(real_plan, left), case


# --------------------------------------------------------------------------
# Albums that must NOT group -- real near misses from the same library
# --------------------------------------------------------------------------

MUST_NOT_GROUP = [
    # KING OF THE MISCHIEVOUS SOUTH vs ... Vol. 2
    ("volume number", "1OUX2HxH2tyqyHMALnYbnG", "6LoDd1G8en4TcqdSg7yqrV"),
    # Tha Carter vs Tha Carter II
    ("roman numeral", "5POcKy926GgzFHZpGptJac", "7slHgsEMuJfnuft5LAPyw6"),
    # King's Disease vs King's Disease II
    ("roman numeral suffix", "5ZQjqg9obFzyGuxGj0mjSi", "6CM5qhYBvpgYNek5kYwuOJ"),
    # Rolling Papers vs Rolling Papers 2
    ("trailing numeral", "6ZOXiVL8rmk2ATHJiFJhiD", "0YFou4SbS16F4GhSADLDfz"),
    # Walkin vs Walkin (Key Glock remix)
    ("remix", "71o4vJ8Jvn43iSqidJGDhO", "4eaBgTvANMYXbm9i1Vyx3q"),
    # Hot Shit (feat. ...) vs the same with [Instrumental]
    ("instrumental", "2qTIltFPwJzsyssGeOwdRO", "1RdCB5mHiyWLYjmoCwHBch"),
    # Biking vs Biking (Solo)
    ("solo", "7yOOPJjNelITCaYMqk8V6r", "46H4UzFHMLnGgpyrj9HpKr"),
    # BORN LIKE THIS vs BORN LIKE THIS (Redux)
    ("redux", "2XfBjZ0ZwKMfwYJDX0JR1O", "6AviMNhTggWeJJ0sFCsc5g"),
    # channel ORANGE vs channel ORANGE (Explicit Version)
    ("explicit version", "392p3shh2jkxUxY2VHvlH8", "623Ef2ZEB3Njklix4PC0Rs"),
    # Tha Carter IV (Deluxe) vs Tha Carter IV (Explicit Version)
    ("explicit beside a recognised marker", "7oc6i8xUILaWwQ5BJMZS3c", "2YL3ddwHuiBGumvzcGHyjT"),
    # Man On The Moon: The End Of Day vs (Int'l Version)
    ("international version", "1OnCqi7IuzjnrOh2ZNvJHd", "6oPPKtAwNNlkW4wwHfQDfM"),
    # Music To Be Murdered By vs ... - Side B (Deluxe Edition)
    ("side b beside deluxe", "4otkd9As6YaxxEkIjXPiZ6", "3MKvhQoFSrR2PrxXXBHe9B"),
    # God Does Like Ugly vs (Preluxe Edition)
    ("unknown marker word", "2tU04u3hxtziB4sOVJKak3", "4YZXGLfofL87b9qR731GwZ"),
    # Finally Famous (Deluxe) vs (10th Anniversary Deluxe Edition Remixed and Remastered)
    ("remixed inside a marker segment", "19DGkH750PrQMMnKqBAxfY", "2wsEKWXcUpVNR2AKprorV3"),
    # SCARING THE HOES vs SCARING THE HOES: DLC PACK
    ("extra words", "2W8QmJ48TFvnkjrrQOHDBR", "20KXgVL9yHtkk6Its2bmpO"),
]


@pytest.mark.parametrize(
    ("case", "left", "right"), MUST_NOT_GROUP, ids=[c[0] for c in MUST_NOT_GROUP]
)
def test_unrecognised_decorations_keep_albums_apart(real_plan, case, left, right):
    assert right not in ids_grouped_together(real_plan, left), case


def test_release_types_never_mix(real_plan):
    """HOPE exists in the library as both an album and a single."""
    assert "3VYKlqWS3zOv1jli94RFKW" not in ids_grouped_together(real_plan, "6zaisPwfcIAfdUGPj3mmGY")
    for group in real_plan.groups:
        assert len({m.album.album_type for m in group.members}) == 1


def test_groups_share_one_primary_artist(real_plan):
    for group in real_plan.groups:
        assert len({m.album.primary_artist_id for m in group.members}) == 1


# --------------------------------------------------------------------------
# Compilations
# --------------------------------------------------------------------------


def test_compilations_are_excluded_and_counted(real_plan, library):
    expected = sum(1 for a in library.albums if a.album_type == "compilation")
    assert expected == 24
    assert real_plan.excluded_reasons == {"compilation": expected}
    assert real_plan.excluded_count == expected


def test_a_compilation_never_appears_in_a_group(real_plan):
    for group in real_plan.groups:
        for member in group.members:
            assert member.album.album_type != "compilation"


def test_compilation_exclusion_beats_an_otherwise_matching_title(real_plan):
    """"The Slim Shady LP (Expanded Edition)" is filed as a compilation."""
    assert group_of(real_plan, "10nO3EJJDMm6j6d2uK3Jah") is None
    assert group_of(real_plan, "0vE6mttRTBXRe9rKghyr1l") is None


# --------------------------------------------------------------------------
# Keeper selection
# --------------------------------------------------------------------------

# (case, any album id in the group, id of the album that must be kept)
MUST_KEEP = [
    ("deluxe over plain", "5CnpZV3q5BcESefcB3WJmz", "2Wiyo7LzdeBCsVZiRA6vVZ"),
    ("expanded over plain", "78Fgb88MY0ECc4GVMejqTg", "13heDPCGwoIdufMoHIyjmh"),
    ("remastered over plain", "6uj0z3xtvQ8Y0UWevdQlIx", "5SBHID8qGG3x52zgoh2ilz"),
    ("complete over deluxe", "7oc6i8xUILaWwQ5BJMZS3c", "0pFydyko4Iw450abXlDPpp"),
    # Watching Movies: (10th Anniversary) over (Deluxe Edition)
    ("anniversary over deluxe", "3T02fCxAjApu18taJLLbyN", "0Wf65emw9eAwbjJ45gMMqp"),
    ("anniversary over plain", "7viNUmZZ8ztn2UB4XB3jIL", "2fSAC0ZiYnwKfzLEvyaMm8"),
]


@pytest.mark.parametrize(("case", "member", "keeper"), MUST_KEEP, ids=[c[0] for c in MUST_KEEP])
def test_richest_edition_is_the_proposed_keeper(real_plan, case, member, keeper):
    group = group_of(real_plan, member)
    assert group is not None, case
    assert group.keeper_id == keeper, case
    assert group.keeper.is_keeper
    assert all(not m.is_keeper for m in group.removals)


def test_rank_tie_breaks_on_track_count(real_plan):
    """Both BLOOD ON MY NIKEZ releases rank plain; one carries two tracks."""
    group = group_of(real_plan, "6XJQyWSjUaPb0M30hX2QU8")
    assert group is not None
    assert {m.edition_rank for m in group.members} == {0}
    assert group.keeper.album.total_tracks == 2
    assert group.keeper_id == "6XJQyWSjUaPb0M30hX2QU8"


def test_rank_tie_then_track_tie_breaks_on_release_date(real_plan):
    """Both Sundown singles rank plain with one track; one is a day later."""
    group = group_of(real_plan, "3Bhw6mGopsWHhS4OlCGDe3")
    assert group is not None
    assert {m.edition_rank for m in group.members} == {0}
    assert {m.album.total_tracks for m in group.members} == {1}
    assert group.keeper_id == "3Bhw6mGopsWHhS4OlCGDe3"
    assert group.keeper.album.release_date == "2024-01-26"


def test_partial_release_dates_do_not_outrank_a_full_one(real_plan):
    """good kid, m.A.A.d city's plain release carries a year-only date."""
    group = group_of(real_plan, "0Oq3mWfexhsjUh0aNNBB5u")
    assert group is not None
    assert group.keeper_id == "748dZDqSZy6aPXKcI9H80u"


# --------------------------------------------------------------------------
# Audit fields
# --------------------------------------------------------------------------


def test_every_group_carries_its_key_and_the_decorations_it_ignored(real_plan):
    group = group_of(real_plan, "5CnpZV3q5BcESefcB3WJmz")
    assert group.key.normalized_title
    assert group.key.primary_artist_id == "5K4W6rqBFWDnAN6FQUkS6x"
    assert group.key.album_type == "album"
    assert "Deluxe" in group.ignored_decorations

    anniversary = group_of(real_plan, "2fSAC0ZiYnwKfzLEvyaMm8")
    assert "10 Year Anniversary Edition" in anniversary.ignored_decorations


def test_a_group_of_identical_titles_ignored_nothing(real_plan):
    group = group_of(real_plan, "78iX7tMceN0FsnmabAtlOC")
    assert group.ignored_decorations == ()


def test_ignored_decorations_are_reported_for_every_member_that_had_one(real_plan):
    group = group_of(real_plan, "3T02fCxAjApu18taJLLbyN")
    per_member = {m.album.id: m.ignored_decorations for m in group.members}
    assert per_member["3T02fCxAjApu18taJLLbyN"] == ("Deluxe Edition",)
    assert per_member["0Wf65emw9eAwbjJ45gMMqp"] == ("10th Anniversary",)


# --------------------------------------------------------------------------
# Asserted-distinct pairs
# --------------------------------------------------------------------------


def test_a_fully_judged_group_disappears_from_the_plan(library, real_plan):
    pair = frozenset({"5CnpZV3q5BcESefcB3WJmz", "2Wiyo7LzdeBCsVZiRA6vVZ"})
    donda_ids = ids_grouped_together(real_plan, "2Wiyo7LzdeBCsVZiRA6vVZ")
    assert len(donda_ids) == 3  # the Donda group has a third member

    all_donda_pairs = frozenset(
        frozenset({a, b}) for a in donda_ids for b in donda_ids if a < b
    )
    suppressed = plan(library, asserted_distinct=all_donda_pairs)
    assert group_of(suppressed, "2Wiyo7LzdeBCsVZiRA6vVZ") is None
    assert suppressed.suppressed_group_count == 1
    assert len(suppressed.groups) == len(real_plan.groups) - 1

    # One judged pair out of three is not enough to drop the group.
    partial = plan(library, asserted_distinct=frozenset({pair}))
    still_there = group_of(partial, "2Wiyo7LzdeBCsVZiRA6vVZ")
    assert still_there is not None
    assert {m.album.id for m in still_there.members} == donda_ids
    assert still_there.suppressed_pair_count == 1
    assert partial.suppressed_group_count == 0


def test_suppressing_one_group_leaves_the_others_alone(library, real_plan):
    pair = frozenset({"6uj0z3xtvQ8Y0UWevdQlIx", "5SBHID8qGG3x52zgoh2ilz"})
    suppressed = plan(library, asserted_distinct=frozenset({pair}))
    assert group_of(suppressed, "5SBHID8qGG3x52zgoh2ilz") is None
    assert group_of(suppressed, "5CnpZV3q5BcESefcB3WJmz") is not None


def test_planning_defaults_to_no_asserted_distinct_pairs(library):
    assert plan(library) == plan(library, asserted_distinct=frozenset())


# --------------------------------------------------------------------------
# Plan-level reporting
# --------------------------------------------------------------------------


def test_the_plan_counts_what_it_scanned_and_what_it_proposes(real_plan, library):
    assert real_plan.total_albums_scanned == len(library.albums) == 1281
    assert real_plan.proposed_removal_count == sum(len(g.members) - 1 for g in real_plan.groups)
    assert len(real_plan.proposed_removal_ids) == real_plan.proposed_removal_count
    removals = set(real_plan.proposed_removal_ids)
    for group in real_plan.groups:
        assert group.keeper_id not in removals


def test_no_album_is_proposed_for_removal_twice(real_plan):
    ids = real_plan.proposed_removal_ids
    assert len(ids) == len(set(ids))


def test_every_group_has_at_least_two_members(real_plan):
    assert real_plan.groups
    for group in real_plan.groups:
        assert len(group.members) >= 2


def test_planning_is_deterministic(library):
    first = plan(library)
    second = plan(library)
    assert [g.key for g in first.groups] == [g.key for g in second.groups]
    assert [g.keeper_id for g in first.groups] == [g.keeper_id for g in second.groups]


# --------------------------------------------------------------------------
# Cases the real library does not contain
# --------------------------------------------------------------------------


def album(
    album_id: str,
    name: str,
    artist_id: str = "artist-1",
    artist_name: str = "An Artist",
    album_type: str = "album",
    total_tracks: int = 10,
    release_date: str = "2000-01-01",
) -> SavedAlbum:
    return SavedAlbum(
        id=album_id,
        name=name,
        artists=(Artist(id=artist_id, name=artist_name),),
        album_type=album_type,
        release_date=release_date,
        release_date_precision="day",
        total_tracks=total_tracks,
    )


def snapshot(*albums: SavedAlbum) -> LibrarySnapshot:
    return LibrarySnapshot(fetched_at="2026-01-01T00:00:00+00:00", albums=albums)


def test_the_same_title_under_two_artists_never_merges():
    result = plan(
        snapshot(
            album("a", "Greatest Album", artist_id="artist-1"),
            album("b", "Greatest Album", artist_id="artist-2"),
        )
    )
    assert result.groups == ()


def test_artists_are_matched_by_id_not_by_name():
    """Identical artist names, different ids: two different artists."""
    result = plan(
        snapshot(
            album("a", "Greatest Album", artist_id="artist-1", artist_name="The Band"),
            album("b", "Greatest Album", artist_id="artist-2", artist_name="The Band"),
        )
    )
    assert result.groups == ()


def test_the_same_artist_under_two_spellings_still_groups():
    """The name text differs; the id does not."""
    result = plan(
        snapshot(
            album("a", "Greatest Album", artist_name="Beyoncé"),
            album("b", "Greatest Album (Deluxe Edition)", artist_name="Beyonce"),
        )
    )
    assert len(result.groups) == 1
    assert result.groups[0].keeper_id == "b"


def test_a_live_recording_is_not_an_edition():
    result = plan(
        snapshot(
            album("a", "Greatest Album"),
            album("b", "Greatest Album (Live)"),
        )
    )
    assert result.groups == ()


@pytest.mark.parametrize(
    "decoration",
    ["Live", "Acoustic", "Instrumental", "Demo", "Remixes", "Karaoke", "Radio Edit", "Soundtrack"],
)
def test_unrecognised_markers_never_merge(decoration):
    result = plan(
        snapshot(
            album("a", "Greatest Album"),
            album("b", f"Greatest Album ({decoration})"),
        )
    )
    assert result.groups == (), decoration


def test_the_richest_of_many_editions_is_kept():
    result = plan(
        snapshot(
            album("plain", "Greatest Album"),
            album("special", "Greatest Album (Special Edition)"),
            album("remaster", "Greatest Album (2011 Remaster)"),
            album("deluxe", "Greatest Album (Deluxe Edition)"),
            album("super", "Greatest Album (Super Deluxe Edition)"),
        )
    )
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.keeper_id == "super"
    assert [m.album.id for m in group.members] == [
        "super",
        "deluxe",
        "remaster",
        "special",
        "plain",
    ]


def test_a_bonus_track_version_outranks_a_plain_edition():
    result = plan(
        snapshot(
            album("plain", "Greatest Album"),
            album("bonus", "Greatest Album (Bonus Track Version)"),
        )
    )
    assert result.groups[0].keeper_id == "bonus"


def test_a_trailing_year_is_ignored_when_comparing():
    result = plan(
        snapshot(
            album("plain", "Greatest Album"),
            album("dated", "Greatest Album (1969)"),
        )
    )
    assert len(result.groups) == 1
    assert "1969" in result.groups[0].ignored_decorations


def test_equal_rank_and_tracks_prefers_the_later_release():
    result = plan(
        snapshot(
            album("older", "Greatest Album", release_date="1999-01-01"),
            album("newer", "Greatest Album", release_date="2005-06-01"),
        )
    )
    assert result.groups[0].keeper_id == "newer"


def test_equal_rank_prefers_more_tracks_even_over_a_later_release():
    result = plan(
        snapshot(
            album("fuller", "Greatest Album", total_tracks=20, release_date="1999-01-01"),
            album("newer", "Greatest Album", total_tracks=12, release_date="2005-06-01"),
        )
    )
    assert result.groups[0].keeper_id == "fuller"


def test_an_empty_library_plans_nothing():
    result = plan(snapshot())
    assert result.groups == ()
    assert result.total_albums_scanned == 0
    assert result.excluded_reasons == {}
