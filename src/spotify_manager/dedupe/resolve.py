"""From a submitted approval to the exact set of albums to remove.

`resolve_decisions` is the second pure seam, and it exists for one reason: it is the
only place where a mis-mapped selection becomes an irreversible deletion. It takes a
plan and a decision payload as plain data and returns the deletion set, so it can be
driven directly by tests with nothing irreversible attached. It imports no
infrastructure, opens no socket, writes no file and reads no clock.

The bias of the whole tool -- an ambiguous case must leave the library alone -- is
enforced here twice over:

* A group nobody decided about is treated as skipped, so silence removes nothing.
* A payload that names a group or an album the plan does not contain is an error,
  not a shrug. Silently ignoring an unrecognised id is exactly how the wrong album
  gets deleted: the id we could not place is evidence that the page and the plan
  disagree, and the only safe response to that is to refuse the whole submission.

Groups are addressed by their position in `plan.groups`, which is what the report
writes into `data-group-index`. Positions are stable for the lifetime of one plan,
which is the only lifetime an approval has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import SpotifyManagerError
from .models import DedupePlan, GroupKey, SavedAlbum

#: The two things a reviewer can say about a group.
RESOLVE = "resolve"
SKIP = "skip"
ACTIONS = (RESOLVE, SKIP)


class ApprovalError(SpotifyManagerError):
    """The submitted decisions do not describe the plan they claim to describe."""


@dataclass(frozen=True)
class GroupDecision:
    """One group's verdict: keep these members, or skip the group entirely."""

    action: str
    keep: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalPayload:
    """The decisions a reviewer submitted, keyed by group index."""

    groups: dict[int, GroupDecision]

    @classmethod
    def from_raw(cls, raw: Any) -> ApprovalPayload:
        """Parse the JSON document the report posts, rejecting anything malformed.

        Parsing is separated from resolving so that a broken payload fails while it
        is still only a dictionary, before any album id has been read as an
        instruction.
        """
        if not isinstance(raw, dict):
            raise ApprovalError("Decisions payload must be a JSON object.")
        raw_groups = raw.get("groups")
        if not isinstance(raw_groups, dict):
            raise ApprovalError("Decisions payload must have a 'groups' object.")

        groups: dict[int, GroupDecision] = {}
        for raw_index, raw_decision in raw_groups.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                raise ApprovalError(
                    f"Decisions name group {raw_index!r}, which is not a group index."
                ) from None
            if not isinstance(raw_decision, dict):
                raise ApprovalError(f"Decision for group {index} is not an object.")
            action = raw_decision.get("action", RESOLVE)
            if action not in ACTIONS:
                raise ApprovalError(
                    f"Decision for group {index} has action {action!r}; "
                    f"expected one of {', '.join(ACTIONS)}."
                )
            keep = raw_decision.get("keep", [])
            if not isinstance(keep, list) or not all(isinstance(i, str) for i in keep):
                raise ApprovalError(
                    f"Decision for group {index} must list kept album ids as strings."
                )
            groups[index] = GroupDecision(action=action, keep=tuple(keep))
        return cls(groups=groups)


@dataclass(frozen=True)
class RestoreAlbum:
    """One album in the restore payload: its id, plus enough to recognise it.

    The id alone is what re-saving needs. The rest is there so a restore file read
    six months later is legible to a human deciding whether to use it.
    """

    id: str
    name: str
    artists: str
    release_date: str
    total_tracks: int

    @classmethod
    def of(cls, album: SavedAlbum) -> RestoreAlbum:
        return cls(
            id=album.id,
            name=album.name,
            artists=album.artist_names,
            release_date=album.release_date,
            total_tracks=album.total_tracks,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "artists": self.artists,
            "release_date": self.release_date,
            "total_tracks": self.total_tracks,
        }


@dataclass(frozen=True)
class SkippedGroup:
    """A group the reviewer rejected, and the pair judgements that implies."""

    key: GroupKey
    album_ids: tuple[str, ...]

    @property
    def asserted_distinct_pairs(self) -> tuple[frozenset[str], ...]:
        """Every unordered pair of members, each now judged *not* duplicates.

        Pair-level rather than group-level, so that adding a new album to this group
        later still surfaces its own unjudged comparisons. Persisting these is the
        ledger's job, not this function's.
        """
        ids = self.album_ids
        return tuple(
            frozenset((ids[a], ids[b]))
            for a in range(len(ids))
            for b in range(a + 1, len(ids))
        )


@dataclass(frozen=True)
class Resolution:
    """What the reviewer's decisions mean, as data. Nothing here has happened yet."""

    to_delete: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    skipped_groups: tuple[SkippedGroup, ...] = ()
    restore_albums: tuple[RestoreAlbum, ...] = ()

    @property
    def asserted_distinct_pairs(self) -> tuple[frozenset[str], ...]:
        """Every pair the skipped groups assert to be distinct records."""
        return tuple(
            pair for group in self.skipped_groups for pair in group.asserted_distinct_pairs
        )

    def restore_document(self, *, created_at: str) -> dict[str, Any]:
        """The restore file's contents, given a timestamp the caller supplies.

        The timestamp is an argument rather than a clock read so this stays pure.
        """
        return {
            "created_at": created_at,
            "album_ids": list(self.to_delete),
            "albums": [album.as_dict() for album in self.restore_albums],
        }


def resolve_decisions(plan: DedupePlan, approval: ApprovalPayload) -> Resolution:
    """Turn a plan plus a reviewer's decisions into the exact set of ids to delete.

    Pure: a function of its two arguments and nothing else.

    Raises:
        ApprovalError: if the payload names a group index the plan does not have, or
            names a kept album that is not a member of the group it was filed under.
            Both mean the submitted page and this plan are not the same plan, and
            deleting on that basis is precisely the mistake this seam exists to
            prevent.
    """
    known = range(len(plan.groups))
    for index in sorted(approval.groups):
        if index not in known:
            raise ApprovalError(
                f"Decisions name group {index}, but this plan has "
                f"{len(plan.groups)} group(s). Nothing was changed."
            )

    to_delete: list[str] = []
    kept: list[str] = []
    skipped: list[SkippedGroup] = []
    restore: list[RestoreAlbum] = []

    for index, group in enumerate(plan.groups):
        member_ids = tuple(m.album.id for m in group.members)
        # No decision means the reviewer never said to remove anything here.
        decision = approval.groups.get(index, GroupDecision(action=SKIP))

        unknown = [album_id for album_id in decision.keep if album_id not in member_ids]
        if unknown:
            raise ApprovalError(
                f"Decisions for group {index} keep album(s) "
                f"{', '.join(sorted(unknown))}, which are not in that group. "
                "Nothing was changed."
            )

        if decision.action == SKIP:
            skipped.append(SkippedGroup(key=group.key, album_ids=member_ids))
            kept.extend(member_ids)
            continue

        keep = set(decision.keep)
        for member in group.members:
            if member.album.id in keep:
                kept.append(member.album.id)
            else:
                to_delete.append(member.album.id)
                restore.append(RestoreAlbum.of(member.album))

    return Resolution(
        to_delete=tuple(to_delete),
        kept=tuple(kept),
        skipped_groups=tuple(skipped),
        restore_albums=tuple(restore),
    )


def approve_plan_unmodified(plan: DedupePlan) -> ApprovalPayload:
    """The payload equivalent to accepting every proposal exactly as planned.

    Useful to tests, and the thing the report's pre-checked boxes reproduce.
    """
    return ApprovalPayload(
        groups={
            index: GroupDecision(action=RESOLVE, keep=(group.keeper_id,))
            for index, group in enumerate(plan.groups)
        }
    )
