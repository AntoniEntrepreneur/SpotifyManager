"""Parsing a restore file's contents: the pure seam, tested with plain values.

`RestoreDocument.from_raw` never touches a filesystem -- that is `restore_cmd`'s job,
covered in `test_restore_cmd.py` -- so every case here is a JSON-shaped Python value
in, a `RestoreDocument` or a `RestoreDocumentError` out.
"""

from __future__ import annotations

import pytest

from spotify_manager.dedupe.restore import RestoreDocument, RestoreDocumentError


def test_a_well_formed_document_yields_its_album_ids():
    raw = {
        "created_at": "2026-09-03T14:30:00+00:00",
        "album_ids": ["a1", "a2", "a3"],
        "albums": [{"id": "a1", "name": "Some Album"}],
    }
    document = RestoreDocument.from_raw(raw)
    assert document.album_ids == ("a1", "a2", "a3")


def test_an_empty_album_ids_list_is_valid_and_means_nothing_to_restore():
    document = RestoreDocument.from_raw({"album_ids": []})
    assert document.album_ids == ()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "not a dict",
        42,
    ],
)
def test_a_document_that_is_not_a_json_object_is_rejected(raw):
    with pytest.raises(RestoreDocumentError, match="JSON object"):
        RestoreDocument.from_raw(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"album_ids": None},
        {"album_ids": "a1"},
        {"album_ids": {"a1": True}},
        {"album_ids": [1, 2]},
        {"album_ids": ["a1", ""]},
        {"album_ids": ["a1", None]},
    ],
)
def test_a_malformed_album_ids_field_is_rejected(raw):
    with pytest.raises(RestoreDocumentError, match="album_ids"):
        RestoreDocument.from_raw(raw)
