"""The `dedupe` command's own logic: what an interrupt during review means, what the
terminal summary says, and where the archived report goes.

`review`'s server wiring is exercised against a fake `ApprovalServer` (patched in
place of the real one) so the one behaviour under test -- what happens when Ctrl-C
lands after a decision has already started being applied -- can be driven precisely
and without a real socket or a real deletion. `format_results` and `archive_report`
are exercised directly against plain data and a real filesystem.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from spotify_manager.commands import dedupe_cmd
from spotify_manager.config import Config
from spotify_manager.dedupe.execute import Applied, BatchOutcome, ExecutionResult
from spotify_manager.dedupe.models import Artist, LibrarySnapshot, SavedAlbum
from spotify_manager.dedupe.planner import plan as build_plan
from spotify_manager.dedupe.resolve import (
    ApprovalPayload,
    RestoreAlbum,
    resolve_decisions,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        client_id="id",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        state_dir=tmp_path / "state",
    )


def _album(album_id: str, name: str) -> SavedAlbum:
    return SavedAlbum(
        id=album_id,
        name=name,
        artists=(Artist(id="artist-1", name="An Artist"),),
        album_type="album",
        release_date="2000-01-01",
        release_date_precision="day",
        total_tracks=10,
    )


def _plan_with_one_group():
    snapshot = LibrarySnapshot(
        fetched_at="2026-01-01T00:00:00+00:00",
        albums=(
            _album("plain", "Greatest Album"),
            _album("deluxe", "Greatest Album (Deluxe Edition)"),
        ),
    )
    return build_plan(snapshot)


def _payload_approving_everything(dedupe_plan) -> dict:
    return {
        "groups": {
            str(index): {
                "action": "resolve",
                "keep": [group.keeper_id],
            }
            for index, group in enumerate(dedupe_plan.groups)
        }
    }


# --------------------------------------------------------------------------
# review(): what a Ctrl-C means once a decision has started being applied
# --------------------------------------------------------------------------


def _fake_approval_server_class(raw: Any) -> type:
    """Build a fake `ApprovalServer` that mimics only what `review` uses: the
    context manager protocol, `.url`, `is_applying`, and `wait_for_decision`.

    Construction immediately starts a background "request thread" that calls the
    real `interpret` (with `raw`, captured by closure) after a short delay --
    exactly as a POST already in flight would be doing while the main thread is
    still blocked in `wait_for_decision`. The first call to `wait_for_decision`
    raises `KeyboardInterrupt`, simulating Ctrl-C landing while that apply is
    already running; the second call waits for it to actually finish and returns
    the true result.
    """

    class _FakeApprovalServer:
        def __init__(self, html, *, interpret, render_results=None, port=0, host="127.0.0.1"):
            self._render_results = render_results
            self.url = "http://fake.invalid/"
            self._applying = True
            self._finished = threading.Event()
            self._result: Any = None
            self._first_wait = True

            def worker() -> None:
                time.sleep(0.15)
                self._result = interpret(raw)
                self._finished.set()

            threading.Thread(target=worker, daemon=True).start()

        @property
        def is_applying(self) -> bool:
            return self._applying

        def wait_for_decision(self) -> Any:
            if self._first_wait:
                self._first_wait = False
                raise KeyboardInterrupt
            self._finished.wait(timeout=5)
            return self._result

        def __enter__(self) -> "_FakeApprovalServer":
            return self

        def __exit__(self, *exc_info: object) -> None:
            pass

    return _FakeApprovalServer


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def delete_albums(self, ids: list[str]) -> None:
        self.calls.append(list(ids))


def test_a_ctrl_c_after_apply_has_started_reports_the_true_outcome_not_abandonment(
    tmp_path, monkeypatch, capsys
):
    """Before the fix, `review` caught every `KeyboardInterrupt` the same way and
    printed "Nothing in your library was changed" unconditionally -- even though,
    in the real server, the interrupt can only land on the main thread while a
    POST already started applying decisions keeps running on its own thread. This
    pins the honest behaviour: an interrupt after the apply has started must wait
    for the true result and report that, never a blanket "nothing changed"."""
    dedupe_plan = _plan_with_one_group()
    config = _config(tmp_path)
    client = _StubClient()
    raw = _payload_approving_everything(dedupe_plan)

    monkeypatch.setattr(dedupe_cmd, "ApprovalServer", _fake_approval_server_class(raw))
    monkeypatch.setattr(dedupe_cmd, "build_client", lambda *a, **k: client)
    monkeypatch.setattr(dedupe_cmd.webbrowser, "open", lambda *a, **k: None)

    applied = dedupe_cmd.review(
        dedupe_plan,
        config=config,
        ledger_file=tmp_path / "ledger.json",
        port=0,
        open_browser=False,
        verbose=False,
    )
    assert applied is not None, "an in-flight apply must not be reported as abandoned"
    assert client.calls, "the real deletion must actually have run"
    assert applied.result.removed_count == 1

    out = capsys.readouterr().out
    assert "Ctrl-C received" in out
    assert "Run abandoned" not in out


def test_a_ctrl_c_before_any_apply_starts_is_reported_as_abandoned(tmp_path, monkeypatch, capsys):
    """The other half of the same behaviour: an interrupt that lands before any
    POST has started applying anything is the safe case, and must still say so."""

    dedupe_plan = _plan_with_one_group()
    config = _config(tmp_path)

    class _NeverAppliedFakeServer:
        """No POST ever arrived: `is_applying` stays false and the interrupt is
        the only thing that happens."""

        def __init__(self, html, *, interpret, render_results=None, port=0, host="127.0.0.1"):
            self.url = "http://fake.invalid/"

        @property
        def is_applying(self) -> bool:
            return False

        def wait_for_decision(self) -> Any:
            raise KeyboardInterrupt

        def __enter__(self) -> "_NeverAppliedFakeServer":
            return self

        def __exit__(self, *exc_info: object) -> None:
            pass

    monkeypatch.setattr(dedupe_cmd, "ApprovalServer", _NeverAppliedFakeServer)
    monkeypatch.setattr(dedupe_cmd, "build_client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("build_client must not be reached when nothing was ever applied")
    ))
    monkeypatch.setattr(dedupe_cmd.webbrowser, "open", lambda *a, **k: None)

    applied = dedupe_cmd.review(
        dedupe_plan,
        config=config,
        ledger_file=tmp_path / "ledger.json",
        port=0,
        open_browser=False,
        verbose=False,
    )

    assert applied is None
    out = capsys.readouterr().out
    assert "Run abandoned. Nothing in your library was changed." in out


# --------------------------------------------------------------------------
# format_results
# --------------------------------------------------------------------------


def _resolution_approving_everything():
    """The one-group plan, fully approved: `deluxe` is kept, `plain` is removed."""
    dedupe_plan = _plan_with_one_group()
    raw = _payload_approving_everything(dedupe_plan)
    return resolve_decisions(dedupe_plan, ApprovalPayload.from_raw(raw))


def _resolution_skipping_everything():
    dedupe_plan = _plan_with_one_group()
    raw = {
        "groups": {
            str(index): {"action": "skip", "keep": []} for index in range(len(dedupe_plan.groups))
        }
    }
    return resolve_decisions(dedupe_plan, ApprovalPayload.from_raw(raw))


def _result(*batches: BatchOutcome, requested: tuple[str, ...], restore: Path | None):
    """An `ExecutionResult` built the only way the real one is ever built.

    The classifications are derived from the batches rather than stored, so a test
    that set `removed_ids` directly would be asserting against a shape the executor
    cannot actually produce.
    """
    return ExecutionResult(
        batches=batches,
        requested_ids=requested,
        restore_path=restore,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_format_results_reports_a_clean_run():
    result = _result(
        BatchOutcome(number=1, album_ids=("plain",), status="succeeded", attempts=1),
        requested=("plain",),
        restore=Path("/tmp/restores/restore-x.json"),
    )
    applied = Applied(
        resolution=_resolution_approving_everything(), result=result, recorded_pairs=0
    )

    text = dedupe_cmd.format_results(applied)

    assert "approved for removal   1" in text
    assert "removed                1" in text
    assert "failed                 0" in text
    assert "never attempted        0" in text
    assert "Every album you approved was removed." in text
    assert "Restore file: /tmp/restores/restore-x.json" in text
    assert "spotify-manager restore" in text
    assert "THIS RUN DID NOT FINISH" not in text


def test_format_results_reports_nothing_requested():
    """Approving nothing is a complete, successful run -- not a failed one."""
    result = _result(requested=(), restore=None)
    applied = Applied(
        resolution=_resolution_skipping_everything(), result=result, recorded_pairs=1
    )

    text = dedupe_cmd.format_results(applied)

    assert "You approved no removals. Nothing was deleted." in text
    assert "Restore file" not in text
    assert "THIS RUN DID NOT FINISH" not in text


def test_format_results_names_failed_and_never_attempted_albums():
    """A partial run names every album by id, and never rounds a failure away."""
    result = _result(
        BatchOutcome(
            number=1, album_ids=("plain",), status="failed", attempts=4, error="boom"
        ),
        requested=("plain",),
        restore=Path("/tmp/restores/restore-y.json"),
    )
    applied = Applied(
        resolution=_resolution_approving_everything(), result=result, recorded_pairs=0
    )

    text = dedupe_cmd.format_results(applied)

    assert "THIS RUN DID NOT FINISH" in text
    assert "Failed (1)" in text
    assert "plain" in text
    assert "Batch 1 error: boom" in text
    assert "Every album you approved was removed." not in text


def test_format_results_distinguishes_never_attempted_from_failed():
    """The two are not interchangeable: one is unknown, the other is certainly saved."""
    result = _result(
        BatchOutcome(number=1, album_ids=("plain",), status="failed", attempts=4, error="boom"),
        BatchOutcome(number=2, album_ids=("deluxe",), status="never_attempted"),
        requested=("plain", "deluxe"),
        restore=Path("/tmp/restores/restore-z.json"),
    )
    applied = Applied(
        resolution=_resolution_approving_everything(), result=result, recorded_pairs=0
    )

    text = dedupe_cmd.format_results(applied)

    assert "failed                 1" in text
    assert "never attempted        1" in text
    assert "these requests were sent and errored" in text
    assert "no request was ever issued for these; they are still saved" in text


# --------------------------------------------------------------------------
# archive_report
# --------------------------------------------------------------------------


def test_archive_report_writes_the_html_under_the_reports_dir(tmp_path):
    config = _config(tmp_path)

    path = dedupe_cmd.archive_report(config, "<html>hello</html>")

    assert path.parent == config.reports_dir
    assert path.name.startswith("dedupe-report-")
    assert path.read_text(encoding="utf-8") == "<html>hello</html>"


def test_archive_report_honours_a_custom_name(tmp_path):
    config = _config(tmp_path)

    path = dedupe_cmd.archive_report(config, "<html>results</html>", name="dedupe-results")

    assert path.name.startswith("dedupe-results-")


def test_archive_report_creates_the_reports_dir_if_missing(tmp_path):
    config = _config(tmp_path)
    assert not config.reports_dir.exists()

    dedupe_cmd.archive_report(config, "<html></html>")

    assert config.reports_dir.exists()
