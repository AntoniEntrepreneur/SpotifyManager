"""The `unlike-tracks` subcommand's I/O shell: reading the record, then unliking.

The mirror of `test_restore_cmd.py`, one noun over. `load_record_file` is exercised
directly against a real filesystem (a missing file, one that is not JSON, one that
does not describe a run record), and `run` is exercised with `build_client`,
`likes.execute.unlike` and the snapshot cache monkeypatched out, so the two things
this file must prove -- that a bad run record is rejected before anything reaches the
API, and that the Liked Songs snapshot is discarded however the run ended -- are
checked without a network.
"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from spotify_manager.commands import unlike_cmd
from spotify_manager.likes.execute import UnlikeResult
from spotify_manager.likes.record import RecordDocumentError


def test_a_missing_run_record_produces_a_clear_message(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(unlike_cmd.RecordFileNotFoundError, match="Nothing was unliked"):
        unlike_cmd.load_record_file(missing)


def test_a_run_record_that_is_not_json_produces_a_clear_message(tmp_path):
    path = tmp_path / "liked.json"
    path.write_text("this is not json{{{", encoding="utf-8")
    with pytest.raises(RecordDocumentError, match="not valid JSON"):
        unlike_cmd.load_record_file(path)


def test_a_run_record_with_the_wrong_shape_produces_a_clear_message(tmp_path):
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"created_at": "now"}), encoding="utf-8")
    with pytest.raises(RecordDocumentError, match="track_ids"):
        unlike_cmd.load_record_file(path)


def test_a_restore_file_is_not_a_run_record(tmp_path):
    """Handing `unlike-tracks` a `dedupe` restore file must fail on the shape, not
    quietly unlike nothing: the two files live in different directories precisely
    because one holds album ids and the other track ids."""
    path = tmp_path / "restore.json"
    path.write_text(json.dumps({"album_ids": ["a1", "a2"]}), encoding="utf-8")
    with pytest.raises(RecordDocumentError, match="track_ids"):
        unlike_cmd.load_record_file(path)


def test_a_well_formed_run_record_parses_to_its_track_ids(tmp_path):
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1", "t2"]}), encoding="utf-8")
    assert unlike_cmd.load_record_file(path).track_ids == ("t1", "t2")


def _args(path, *, yes=True, dry_run=False) -> argparse.Namespace:
    """Defaults to `--yes`, so the tests that are not about the prompt do not have to
    answer one. The prompt has its own tests below."""
    return argparse.Namespace(
        run_record=path, verbose=False, rate=None, yes=yes, dry_run=dry_run
    )


class _Tty(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# -- the confirmation --------------------------------------------------------

# The rules live in `commands.confirm` and are pinned in full against
# `like-album-tracks`. What is checked here is that the destructive direction is
# actually behind them, and that its wording names the destructive verb.


def test_yes_at_the_prompt_approves():
    assert unlike_cmd.confirmed(10, assume_yes=False, stream=_Tty("yes\n")) is True


@pytest.mark.parametrize("answer", ["no\n", "\n", "y\n", ""])
def test_anything_but_yes_does_not_approve(answer):
    assert unlike_cmd.confirmed(10, assume_yes=False, stream=_Tty(answer)) is False


def test_the_prompt_says_unlike_not_like(capsys):
    """A user typing yes to the wrong question is the accident the wording prevents."""
    unlike_cmd.confirmed(9473, assume_yes=False, stream=_Tty("no\n"))
    asked = capsys.readouterr().out
    assert "Unlike 9473 tracks?" in asked


def test_a_non_terminal_stdin_refuses_rather_than_proceeding(capsys):
    """A redirect must never be able to approve destroying ten thousand likes."""
    assert unlike_cmd.confirmed(9473, assume_yes=False, stream=_Tty("", tty=False)) is False
    err = capsys.readouterr().err
    assert "--yes" in err and "unlike" in err


class FakeCache:
    def __init__(self) -> None:
        self.discarded = False

    def discard(self) -> None:
        self.discarded = True


@pytest.fixture
def cache(monkeypatch) -> FakeCache:
    fake = FakeCache()
    monkeypatch.setattr(unlike_cmd, "liked_tracks_cache", lambda _config: fake)
    return fake


def test_a_malformed_run_record_never_reaches_the_client(tmp_path, monkeypatch):
    """Nothing may be sent to the API before the file has been fully parsed and
    validated: a run that fails to parse must not have called `build_client` or
    `unlike` at all."""
    path = tmp_path / "liked.json"
    path.write_text("not json", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: calls.append("client"))
    monkeypatch.setattr(unlike_cmd, "unlike", lambda *a, **k: calls.append("unlike"))
    monkeypatch.setattr(
        unlike_cmd, "load_config", lambda: pytest.fail("should not need credentials to fail")
    )

    with pytest.raises(RecordDocumentError):
        unlike_cmd.run(_args(path))
    assert calls == []


def test_an_empty_run_record_unlikes_nothing_and_reports_success(tmp_path, monkeypatch):
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": []}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    calls: list[str] = []
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: calls.append("client"))
    monkeypatch.setattr(unlike_cmd, "unlike", lambda *a, **k: calls.append("unlike"))

    assert unlike_cmd.run(_args(path)) == 0
    assert calls == []


def _clean(track_ids):
    from spotify_manager.batching import SUCCEEDED, BatchOutcome

    return UnlikeResult(
        batches=(BatchOutcome(number=1, ids=track_ids, status=SUCCEEDED),),
        requested_ids=track_ids,
    )


def test_a_clean_unlike_run_reports_the_count_and_exits_zero(tmp_path, monkeypatch, cache):
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1", "t2", "t3"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: "fake-client")

    seen = {}

    def fake_unlike(track_ids, client, **kwargs):
        seen["track_ids"] = track_ids
        seen["client"] = client
        return _clean(track_ids)

    monkeypatch.setattr(unlike_cmd, "unlike", fake_unlike)

    exit_code = unlike_cmd.run(_args(path))

    assert seen["track_ids"] == ("t1", "t2", "t3")
    assert seen["client"] == "fake-client"
    assert exit_code == 0
    assert cache.discarded


def test_the_run_record_is_not_deleted_by_the_run_that_used_it(tmp_path, monkeypatch, cache):
    """Un-liking is itself undoable -- by re-running `like-album-tracks` -- and only
    while the list of ids still exists. The file is read, never consumed."""
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: "fake-client")
    monkeypatch.setattr(unlike_cmd, "unlike", lambda ids, client, **k: _clean(ids))

    unlike_cmd.run(_args(path))
    assert path.exists()


def test_a_run_that_did_not_finish_exits_nonzero(tmp_path, monkeypatch, cache):
    from spotify_manager.batching import FAILED, BatchOutcome

    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: "fake-client")
    monkeypatch.setattr(
        unlike_cmd,
        "unlike",
        lambda ids, client, **k: UnlikeResult(
            batches=(BatchOutcome(number=1, ids=ids, status=FAILED, error="boom"),),
            requested_ids=ids,
        ),
    )

    assert unlike_cmd.run(_args(path)) == 1


def test_the_snapshot_is_discarded_even_when_the_run_blows_up(tmp_path, monkeypatch, cache):
    """The run touched Liked Songs before it died, so the snapshot is wrong however
    it ended -- including by Ctrl-C."""
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: "fake-client")

    def boom(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(unlike_cmd, "unlike", boom)

    with pytest.raises(KeyboardInterrupt):
        unlike_cmd.run(_args(path))
    assert cache.discarded


def test_an_empty_record_does_not_discard_a_snapshot_it_did_not_invalidate(
    tmp_path, monkeypatch, cache
):
    """No request went out, so Liked Songs is exactly what the snapshot says it is.
    Throwing it away would cost a full refetch to learn nothing."""
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": []}), encoding="utf-8")
    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())

    assert unlike_cmd.run(_args(path)) == 0
    assert not cache.discarded


def test_a_client_that_cannot_be_built_does_not_cost_a_snapshot(tmp_path, monkeypatch, cache):
    """No request went out, so Liked Songs is still exactly what the snapshot says.
    Discarding it here would turn a failed login into a needless full refetch."""
    from spotify_manager.errors import SpotifyManagerError

    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())

    def no_credentials(*_a, **_k):
        raise SpotifyManagerError("no credentials")

    monkeypatch.setattr(unlike_cmd, "build_client", no_credentials)
    monkeypatch.setattr(unlike_cmd, "unlike", lambda *a, **k: pytest.fail("no client to use"))

    with pytest.raises(SpotifyManagerError):
        unlike_cmd.run(_args(path))
    assert not cache.discarded


def test_format_result_reports_success_cleanly():
    text = unlike_cmd.format_result(UnlikeResult(batches=(), requested_ids=()))
    assert "Every track was unliked." in text


def test_format_result_names_failures_and_never_attempted():
    from spotify_manager.batching import FAILED, NEVER_ATTEMPTED, BatchOutcome

    result = UnlikeResult(
        batches=(
            BatchOutcome(number=1, ids=("f1",), status=FAILED, error="boom"),
            BatchOutcome(number=2, ids=("n1",), status=NEVER_ATTEMPTED),
        ),
        requested_ids=("f1", "n1"),
    )
    text = unlike_cmd.format_result(result)
    assert "THIS RUN DID NOT FINISH" in text
    assert "f1" in text
    assert "n1" in text
    assert "boom" in text


def test_a_declined_prompt_unlikes_nothing_and_exits_nonzero(tmp_path, monkeypatch, cache):
    """The gate that finding #1 asked for: the record's contents are the planner's
    choice, not the user's, so removing likes has to be agreed to."""
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: pytest.fail("not agreed"))
    monkeypatch.setattr(unlike_cmd, "unlike", lambda *a, **k: pytest.fail("not agreed"))
    monkeypatch.setattr(unlike_cmd, "confirmed", lambda *a, **k: False)

    assert unlike_cmd.run(_args(path, yes=False)) == 1
    assert not cache.discarded, "nothing was sent, so the snapshot is still right"


def test_a_dry_run_touches_nothing_and_exits_zero(tmp_path, monkeypatch, cache, capsys):
    path = tmp_path / "liked.json"
    path.write_text(json.dumps({"track_ids": ["t1", "t2"]}), encoding="utf-8")

    monkeypatch.setattr(unlike_cmd, "load_config", lambda: object())
    monkeypatch.setattr(unlike_cmd, "build_client", lambda *a, **k: pytest.fail("dry run"))
    monkeypatch.setattr(unlike_cmd, "unlike", lambda *a, **k: pytest.fail("dry run"))
    monkeypatch.setattr(unlike_cmd, "confirmed", lambda *a, **k: pytest.fail("dry run"))

    assert unlike_cmd.run(_args(path, yes=False, dry_run=True)) == 0
    assert "2 track(s)" in capsys.readouterr().out
    assert not cache.discarded


def test_a_batch_spotify_refused_is_reported_as_still_liked_not_as_unknown():
    """`batching` already knows the difference: every attempt answered 4xx means
    Spotify refused to act, so those tracks are certainly still liked. Collapsing
    that into "may or may not" would throw away the one certainty in a failed run --
    and a missing `user-library-modify` scope 403s every batch, which is exactly the
    case where the user most needs to be told nothing happened."""
    from spotify_manager.batching import FAILED, BatchOutcome

    result = UnlikeResult(
        batches=(
            BatchOutcome(
                number=1, ids=("r1",), status=FAILED, error="ApiError: 403", error_status=403
            ),
            BatchOutcome(number=2, ids=("u1",), status=FAILED, error="Timeout"),
        ),
        requested_ids=("r1", "u1"),
    )
    text = unlike_cmd.format_result(result)

    refused, unknown = text.index("Refused by Spotify"), text.index("\nFailed (")
    assert text.index("r1") > refused and text.index("r1") < unknown
    assert text.index("u1") > unknown
    assert "those tracks are still liked" in text
    assert "may or may not still be liked" in text


def test_a_run_with_no_refusals_prints_no_refused_section():
    """The section exists to carry a certainty; with nothing certain it is noise."""
    from spotify_manager.batching import FAILED, BatchOutcome

    result = UnlikeResult(
        batches=(BatchOutcome(number=1, ids=("u1",), status=FAILED, error="Timeout"),),
        requested_ids=("u1",),
    )
    assert "Refused by Spotify" not in unlike_cmd.format_result(result)


def test_format_result_does_not_print_ten_thousand_ids():
    """A run record can list ten thousand tracks, and a failed batch of them is not a
    thing to paste into a terminal. `like-album-tracks` truncates for the same
    reason -- and the file naming every id is still on disk."""
    from spotify_manager.batching import FAILED, BatchOutcome

    ids = tuple(f"t{i}" for i in range(200))
    result = UnlikeResult(
        batches=(BatchOutcome(number=1, ids=ids, status=FAILED, error="boom"),),
        requested_ids=ids,
    )
    text = unlike_cmd.format_result(result)
    assert text.count("\n  t") == 50
    assert "and 150 more" in text


def test_the_subcommand_is_reachable_from_the_command_line():
    """The one thing every test above takes for granted: that `unlike-tracks` is
    actually wired into the parser, and hands its path through as a Path."""
    from pathlib import Path

    from spotify_manager.cli import build_parser

    args = build_parser().parse_args(["unlike-tracks", "likes/liked-x.json"])
    assert args.func is unlike_cmd.run
    assert args.run_record == Path("likes/liked-x.json")
