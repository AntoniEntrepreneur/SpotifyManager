"""The `restore` subcommand's I/O shell: reading the file, then calling `restore`.

`load_restore_file` is exercised directly against a real filesystem (a missing file,
one that is not JSON, one that does not describe a restore document), and `run` is
exercised with `build_client` and `dedupe.execute.restore` monkeypatched out, so the
one thing this file must prove -- that a bad restore file is rejected before
anything reaches the API -- is checked without a network.
"""

from __future__ import annotations

import argparse
import json

import pytest

from spotify_manager.commands import restore_cmd
from spotify_manager.dedupe.execute import ExecutionResult
from spotify_manager.dedupe.restore import RestoreDocumentError


def test_a_missing_restore_file_produces_a_clear_message(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(restore_cmd.RestoreFileNotFoundError, match="Nothing was restored"):
        restore_cmd.load_restore_file(missing)


def test_a_restore_file_that_is_not_json_produces_a_clear_message(tmp_path):
    path = tmp_path / "restore.json"
    path.write_text("this is not json{{{", encoding="utf-8")
    with pytest.raises(RestoreDocumentError, match="not valid JSON"):
        restore_cmd.load_restore_file(path)


def test_a_restore_file_with_the_wrong_shape_produces_a_clear_message(tmp_path):
    path = tmp_path / "restore.json"
    path.write_text(json.dumps({"created_at": "now"}), encoding="utf-8")
    with pytest.raises(RestoreDocumentError, match="album_ids"):
        restore_cmd.load_restore_file(path)


def test_a_well_formed_restore_file_parses_to_its_album_ids(tmp_path):
    path = tmp_path / "restore.json"
    path.write_text(json.dumps({"album_ids": ["a1", "a2"]}), encoding="utf-8")
    document = restore_cmd.load_restore_file(path)
    assert document.album_ids == ("a1", "a2")


def _args(path) -> argparse.Namespace:
    return argparse.Namespace(restore_file=path, verbose=False)


def test_a_malformed_restore_file_never_reaches_the_client(tmp_path, monkeypatch):
    """Nothing may be sent to the API before the file has been fully parsed and
    validated: a run that fails to parse must not have called `build_client` or
    `restore` at all."""
    path = tmp_path / "restore.json"
    path.write_text("not json", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(restore_cmd, "build_client", lambda *a, **k: calls.append("client"))
    monkeypatch.setattr(restore_cmd, "restore", lambda *a, **k: calls.append("restore"))
    monkeypatch.setattr(
        restore_cmd, "load_config", lambda: pytest.fail("should not need credentials to fail")
    )

    with pytest.raises(RestoreDocumentError):
        restore_cmd.run(_args(path))
    assert calls == []


def test_an_empty_restore_file_restores_nothing_and_reports_success(tmp_path, monkeypatch):
    path = tmp_path / "restore.json"
    path.write_text(json.dumps({"album_ids": []}), encoding="utf-8")

    monkeypatch.setattr(restore_cmd, "load_config", lambda: object())
    calls: list[str] = []
    monkeypatch.setattr(restore_cmd, "build_client", lambda *a, **k: calls.append("client"))
    monkeypatch.setattr(restore_cmd, "restore", lambda *a, **k: calls.append("restore"))

    assert restore_cmd.run(_args(path)) == 0
    assert calls == []


def test_a_clean_restore_run_reports_the_count_and_exits_zero(tmp_path, monkeypatch):
    path = tmp_path / "restore.json"
    path.write_text(json.dumps({"album_ids": ["a1", "a2", "a3"]}), encoding="utf-8")

    monkeypatch.setattr(restore_cmd, "load_config", lambda: object())
    monkeypatch.setattr(restore_cmd, "build_client", lambda *a, **k: "fake-client")

    seen = {}

    def fake_restore(album_ids, client, **kwargs):
        from spotify_manager.dedupe.execute import SUCCEEDED, BatchOutcome

        seen["album_ids"] = album_ids
        seen["client"] = client
        return ExecutionResult(
            batches=(BatchOutcome(number=1, ids=album_ids, status=SUCCEEDED),),
            requested_ids=album_ids,
        )

    monkeypatch.setattr(restore_cmd, "restore", fake_restore)

    exit_code = restore_cmd.run(_args(path))

    assert seen["album_ids"] == ("a1", "a2", "a3")
    assert seen["client"] == "fake-client"
    assert exit_code == 0


def test_format_result_reports_success_cleanly():
    result = ExecutionResult(batches=(), requested_ids=())
    text = restore_cmd.format_result(result)
    assert "Every album was restored." in text


def test_format_result_names_failures_and_never_attempted():
    from spotify_manager.dedupe.execute import FAILED, NEVER_ATTEMPTED, BatchOutcome

    result = ExecutionResult(
        batches=(
            BatchOutcome(number=1, ids=("f1",), status=FAILED, error="boom"),
            BatchOutcome(number=2, ids=("n1",), status=NEVER_ATTEMPTED),
        ),
        requested_ids=("f1", "n1"),
    )
    text = restore_cmd.format_result(result)
    assert "THIS RUN DID NOT FINISH" in text
    assert "f1" in text
    assert "n1" in text
    assert "boom" in text
