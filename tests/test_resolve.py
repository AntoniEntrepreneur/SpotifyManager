"""The resolver: from a submitted approval to the exact set of albums to delete.

This is the seam the spec singles out as irreversible, so it is tested harder than
anything else and entirely without a network, a server or a browser. The claims here
are about outcomes a mistake would make catastrophic: which ids come out, which ids
do not, and what happens when the payload and the plan disagree.

Small synthetic plans are used deliberately. This seam knows nothing about title
normalization -- it maps ticks to ids -- so a fixture full of real albums would
obscure the cases rather than sharpen them. The one exception is the round trip
through the real fixture plan, which pins the claim that matters most: approving an
unmodified plan reproduces that plan's own proposal exactly.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from spotify_manager.dedupe.models import (
    AlbumJudgement,
    Artist,
    DedupePlan,
    DuplicateGroup,
    GroupKey,
    SavedAlbum,
    snapshot_from_raw,
)
from spotify_manager.dedupe.planner import plan as build_plan
from spotify_manager.dedupe.resolve import (
    ApprovalError,
    ApprovalPayload,
    GroupDecision,
    approve_plan_unmodified,
    resolve_decisions,
)

FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"
RESOLVE_MODULE = Path(__file__).parents[1] / "src" / "spotify_manager" / "dedupe" / "resolve.py"


@pytest.fixture(scope="module")
def real_plan() -> DedupePlan:
    return build_plan(snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8"))))


# --------------------------------------------------------------------------
# Small synthetic plans, where the ids under test are the point
# --------------------------------------------------------------------------


def _album(album_id: str) -> SavedAlbum:
    return SavedAlbum(
        id=album_id,
        name=f"Album {album_id}",
        artists=(Artist(id="artist-1", name="A Band"),),
        album_type="album",
        release_date="2019-03-04",
        release_date_precision="day",
        total_tracks=11,
    )


def _group(*album_ids: str, keeper: str | None = None, key: str = "album") -> DuplicateGroup:
    keeper_id = keeper or album_ids[0]
    members = tuple(
        AlbumJudgement(
            album=_album(album_id),
            edition_rank=80 if album_id == keeper_id else 0,
            edition_category="deluxe" if album_id == keeper_id else "plain",
            ignored_decorations=(),
            is_keeper=album_id == keeper_id,
        )
        for album_id in album_ids
    )
    return DuplicateGroup(
        key=GroupKey(normalized_title=key, primary_artist_id="artist-1", album_type="album"),
        members=members,
        keeper_id=keeper_id,
        ignored_decorations=("Deluxe",),
    )


def _plan(*groups: DuplicateGroup) -> DedupePlan:
    return DedupePlan(groups=groups, total_albums_scanned=99)


def _approval(**decisions: GroupDecision) -> ApprovalPayload:
    return ApprovalPayload(groups={int(name[1:]): value for name, value in decisions.items()})


def keep(*ids: str) -> GroupDecision:
    return GroupDecision(action="resolve", keep=tuple(ids))


SKIP = GroupDecision(action="skip")


# --------------------------------------------------------------------------
# The three cases the acceptance criteria name
# --------------------------------------------------------------------------


def test_keeping_a_subset_of_a_group_removes_exactly_the_rest():
    plan = _plan(_group("a", "b", "c", "d"))
    resolution = resolve_decisions(plan, _approval(g0=keep("a", "c")))

    assert resolution.to_delete == ("b", "d")
    assert set(resolution.kept) == {"a", "c"}
    assert resolution.skipped_groups == ()


def test_a_skipped_group_contributes_nothing_to_the_deletion_set():
    plan = _plan(_group("a", "b"), _group("x", "y", key="other"))
    resolution = resolve_decisions(plan, _approval(g0=SKIP, g1=keep("x")))

    assert resolution.to_delete == ("y",)
    assert [g.key.normalized_title for g in resolution.skipped_groups] == ["album"]
    assert resolution.skipped_groups[0].album_ids == ("a", "b")
    # Skipping is a decision to keep the group as it stands, not to forget it.
    assert set(resolution.kept) == {"a", "b", "x"}


def test_approving_an_unmodified_plan_reproduces_the_plans_own_proposal(real_plan: DedupePlan):
    """The whole safety story rests on this: the default is the plan, unchanged."""
    assert len(real_plan.groups) > 1  # the fixture is the real library, not a toy
    resolution = resolve_decisions(real_plan, approve_plan_unmodified(real_plan))

    assert resolution.to_delete == real_plan.proposed_removal_ids
    assert len(resolution.to_delete) == real_plan.proposed_removal_count
    assert set(resolution.kept) == {g.keeper_id for g in real_plan.groups}
    assert resolution.skipped_groups == ()


# --------------------------------------------------------------------------
# The edges either side of those
# --------------------------------------------------------------------------


def test_keeping_every_member_of_a_group_removes_nothing_from_it():
    plan = _plan(_group("a", "b", "c"))
    resolution = resolve_decisions(plan, _approval(g0=keep("a", "b", "c")))

    assert resolution.to_delete == ()
    assert resolution.restore_albums == ()
    assert set(resolution.kept) == {"a", "b", "c"}


def test_keeping_nothing_in_a_group_removes_all_of_it():
    """Allowed, because it is a thing a reviewer can genuinely mean; the report
    warns before submitting, and the resolver simply does as it was told."""
    plan = _plan(_group("a", "b"))
    resolution = resolve_decisions(plan, _approval(g0=keep()))

    assert resolution.to_delete == ("a", "b")
    assert resolution.kept == ()


def test_an_empty_approval_removes_nothing_at_all(real_plan: DedupePlan):
    """Silence is not consent. A group nobody decided about is treated as skipped."""
    resolution = resolve_decisions(real_plan, ApprovalPayload(groups={}))

    assert resolution.to_delete == ()
    assert len(resolution.skipped_groups) == len(real_plan.groups)


def test_a_plan_with_no_groups_resolves_to_nothing():
    resolution = resolve_decisions(DedupePlan(), ApprovalPayload(groups={}))
    assert resolution.to_delete == ()
    assert resolution.skipped_groups == ()


# --------------------------------------------------------------------------
# Disagreement between the payload and the plan
# --------------------------------------------------------------------------


def test_a_decision_for_a_group_the_plan_does_not_have_is_refused():
    """Loudly, and before anything is resolved: an id we cannot place is evidence
    that the page and the plan are not the same plan, and deleting on that basis is
    exactly the mistake this seam exists to prevent."""
    plan = _plan(_group("a", "b"))
    with pytest.raises(ApprovalError) as raised:
        resolve_decisions(plan, _approval(g0=keep("a"), g7=keep("z")))

    assert "group 7" in str(raised.value)
    assert "Nothing was changed" in str(raised.value)


def test_keeping_an_album_that_is_not_in_that_group_is_refused():
    plan = _plan(_group("a", "b"), _group("x", "y", key="other"))
    with pytest.raises(ApprovalError) as raised:
        resolve_decisions(plan, _approval(g0=keep("a", "x")))

    assert "x" in str(raised.value)
    assert "not in that group" in str(raised.value)


def test_refusing_a_bad_payload_happens_before_any_id_is_resolved():
    """The refusal must be all-or-nothing: a good group ahead of a bad one must not
    leak a deletion set out of the failure."""
    plan = _plan(_group("a", "b"), _group("x", "y", key="other"))
    with pytest.raises(ApprovalError):
        resolve_decisions(plan, _approval(g0=keep("a"), g1=keep("nonsense")))


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {},
        {"groups": []},
        {"groups": {"nope": {"action": "resolve", "keep": []}}},
        {"groups": {"0": "resolve"}},
        {"groups": {"0": {"action": "delete_everything", "keep": []}}},
        {"groups": {"0": {"action": "resolve", "keep": "a"}}},
        {"groups": {"0": {"action": "resolve", "keep": [1, 2]}}},
    ],
)
def test_a_malformed_payload_is_refused_while_it_is_still_only_a_dictionary(raw: object):
    with pytest.raises(ApprovalError):
        ApprovalPayload.from_raw(raw)


def test_a_payload_the_report_would_actually_post_parses():
    payload = ApprovalPayload.from_raw(
        {
            "groups": {
                "0": {"action": "resolve", "keep": ["a"]},
                "1": {"action": "skip", "keep": []},
            }
        }
    )
    assert payload.groups[0] == GroupDecision(action="resolve", keep=("a",))
    assert payload.groups[1] == GroupDecision(action="skip", keep=())


# --------------------------------------------------------------------------
# What the resolution carries onward
# --------------------------------------------------------------------------


def test_the_restore_payload_covers_every_album_that_would_be_deleted():
    plan = _plan(_group("a", "b", "c"))
    resolution = resolve_decisions(plan, _approval(g0=keep("a")))

    assert [album.id for album in resolution.restore_albums] == list(resolution.to_delete)
    assert all(album.name and album.artists for album in resolution.restore_albums)
    document = resolution.restore_document(created_at="2026-09-03T14:30:00")
    assert document["album_ids"] == ["b", "c"]
    assert document["created_at"] == "2026-09-03T14:30:00"
    assert [album["id"] for album in document["albums"]] == ["b", "c"]


def test_a_skipped_group_yields_every_pair_of_its_members_as_asserted_distinct():
    """Pair-level, so a later run still asks about a genuinely new comparison."""
    plan = _plan(_group("a", "b", "c"), _group("x", "y", key="other"))
    resolution = resolve_decisions(plan, _approval(g0=SKIP, g1=SKIP))

    assert set(resolution.asserted_distinct_pairs) == {
        frozenset({"a", "b"}),
        frozenset({"a", "c"}),
        frozenset({"b", "c"}),
        frozenset({"x", "y"}),
    }


def test_resolving_a_group_asserts_nothing_about_its_pairs():
    plan = _plan(_group("a", "b"))
    resolution = resolve_decisions(plan, _approval(g0=keep("a")))
    assert resolution.asserted_distinct_pairs == ()


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_resolving_twice_gives_the_same_answer(real_plan: DedupePlan):
    approval = approve_plan_unmodified(real_plan)
    assert resolve_decisions(real_plan, approval) == resolve_decisions(real_plan, approval)


def test_the_resolver_cannot_reach_the_network_the_filesystem_or_the_report():
    """The cheapest guarantee that this is a function of its two arguments."""
    tree = ast.parse(RESOLVE_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    forbidden = ("pathlib", "os", "time", "datetime", "requests", "urllib", "socket", "http")
    for name in imported:
        assert name.lstrip(".").split(".")[0] not in forbidden, f"resolve.py imports {name!r}"
        assert "infra" not in name and "report" not in name, f"resolve.py imports {name!r}"
