"""Reading back a restore file: the exact ids a `restore` run must re-save.

`RestoreDocument.from_raw` is the mirror image of `ApprovalPayload.from_raw` in
`resolve.py`: it turns whatever JSON value a file on disk happened to contain into a
validated tuple of ids, or refuses the whole thing. Parsing is kept separate from
issuing requests for the same reason resolving is kept separate from deleting -- a
malformed file must fail while it is still only a parsed value, before a single
request has gone out, so a restore run either re-saves every id the file names or
none of them. There is no partial result to return.

Imports nothing from `infra`, `requests`, `pathlib`, `time` or `datetime`: reading
the actual bytes off disk and deciding they don't even parse as JSON is the caller's
job (`commands/restore_cmd.py`), so this module stays a function of an already-parsed
value, testable with plain dictionaries and no filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import SpotifyManagerError


class RestoreDocumentError(SpotifyManagerError):
    """The restore file's contents do not describe a restore file."""


@dataclass(frozen=True)
class RestoreDocument:
    """The one thing a restore run needs out of the file: which ids to re-save."""

    album_ids: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: Any) -> RestoreDocument:
        """Parse and fully validate the document `write_restore_file` produced.

        Only `album_ids` is required. `created_at` and `albums` exist for a human
        deciding whether to trust the file, not for this to act on, so their absence
        or malformation is not this function's concern.

        Raises:
            RestoreDocumentError: if `raw` is not a JSON object, has no `album_ids`
                key, or that key is not a list of non-empty strings.
        """
        if not isinstance(raw, dict):
            raise RestoreDocumentError(
                "Restore file must contain a JSON object. Nothing was restored."
            )
        raw_ids = raw.get("album_ids")
        if not isinstance(raw_ids, list) or not all(
            isinstance(album_id, str) and album_id for album_id in raw_ids
        ):
            raise RestoreDocumentError(
                "Restore file must have an 'album_ids' field listing album ids as "
                "non-empty strings. Nothing was restored."
            )
        return cls(album_ids=tuple(raw_ids))
