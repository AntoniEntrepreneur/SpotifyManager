"""The irreversible step, driven against a fake client and a real filesystem.

The spec leaves the I/O shell to be verified by running it, with one exception it
names explicitly: "the mapping from approval to deletion tested independently,
because it is the only place where a mistake is irreversible". This file is the
second half of that -- the resolver decides *what* to delete and is tested in
`test_resolve.py`; this tests *how*, and above all the ordering promise that the
restore file is on disk before the first deletion is issued.

Nothing here touches Spotify. The fake client is not a mock of the API; it is a
stand-in for the one method `execute` calls, and its job is mostly to fail in
specific ways at specific moments so the classification can be checked against a
known truth.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spotify_manager.dedupe.execute import (
    FAILED,
    NEVER_ATTEMPTED,
    SUCCEEDED,
    ExecutionResult,
    RestoreFileError,
    batches_of,
    execute,
    restore_command_for,
)
from spotify_manager.dedupe.resolve import Resolution, RestoreAlbum
from spotify_manager.infra.client import ID_BATCH_LIMIT

NOW = datetime(2026, 9, 3, 14, 30, 0, tzinfo=timezone.utc)


def album(index: int) -> RestoreAlbum:
    return RestoreAlbum(
        id=f"alb{index:04d}",
        name=f"Album {index} (Deluxe Edition)",
        artists="Some Artist",
        release_date="2019-09-27",
        total_tracks=12,
    )


def resolution_of(count: int, *, kept: int = 0, skipped=()) -> Resolution:
    albums = tuple(album(i) for i in range(count))
    return Resolution(
        to_delete=tuple(a.id for a in albums),
        kept=tuple(f"keep{i}" for i in range(kept)),
        skipped_groups=tuple(skipped),
        restore_albums=albums,
    )


class FakeClient:
    """Stands in for `SpotifyClient.delete_albums`, one call per batch.

    `script` maps a 1-based call number to what that call should do: `None` (or a
    missing entry) succeeds, an exception instance is raised, and a callable is
    invoked and may raise.
    """

    def __init__(self, script: dict[int, object] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._script = script or {}

    def delete_albums(self, ids: list[str]) -> None:
        self.calls.append(list(ids))
        action = self._script.get(len(self.calls))
        if action is None:
            return
        if callable(action):
            action(list(ids))
            return
        raise action

    @property
    def deleted_ids(self) -> list[str]:
        return [album_id for call in self.calls for album_id in call]


def run(resolution: Resolution, client: FakeClient, tmp_path: Path, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("sleep", lambda seconds: None)
    return execute(resolution, client, restores_dir=tmp_path / "restores", **kwargs)


# -- the ordering promise ----------------------------------------------------


def test_the_restore_file_is_on_disk_and_complete_before_the_first_delete(tmp_path):
    """The single most important assertion in this feature.

    The check runs *inside* the first delete call, so it cannot pass by accident of
    ordering later: if the file were written after the deletions, or written empty and
    filled in afterwards, or left in an OS buffer, this fails.
    """
    resolution = resolution_of(120)
    seen: dict[str, object] = {}

    def inspect(ids: list[str]) -> None:
        found = list((tmp_path / "restores").glob("restore-*.json"))
        assert len(found) == 1, "no restore file existed when the first delete was issued"
        document = json.loads(found[0].read_text(encoding="utf-8"))
        seen["path"] = found[0]
        seen["document"] = document

    client = FakeClient({1: inspect})
    result = run(resolution, client, tmp_path)

    document = seen["document"]
    # Not merely present: complete. Every album this run will remove, not just the
    # ones in the batch that happened to be going out first.
    assert document["album_ids"] == list(resolution.to_delete)
    assert len(document["albums"]) == 120
    assert document["created_at"] == NOW.isoformat(timespec="seconds")
    assert document["albums"][0]["name"] == "Album 0 (Deluxe Edition)"
    assert seen["path"] == result.restore_path


def test_the_restore_file_is_named_for_the_moment_of_the_run(tmp_path):
    result = run(resolution_of(1), FakeClient(), tmp_path)
    assert result.restore_path.name == "restore-2026-09-03T14-30-00.json"
    assert result.restore_path.parent == tmp_path / "restores"


def test_nothing_is_deleted_when_the_restore_file_cannot_be_written(tmp_path):
    """No recovery, no removal. The bias of the whole tool, at its sharpest point."""
    blocked = tmp_path / "restores"
    blocked.write_text("this is a file, not a directory", encoding="utf-8")
    client = FakeClient()

    with pytest.raises(RestoreFileError) as raised:
        run(resolution_of(10), client, tmp_path)

    assert client.calls == []
    assert "Nothing was deleted" in str(raised.value)


# -- batching ----------------------------------------------------------------


def test_removals_go_out_in_the_largest_batches_the_api_permits(tmp_path):
    resolution = resolution_of(120)
    client = FakeClient()
    result = run(resolution, client, tmp_path)

    assert [len(call) for call in client.calls] == [50, 50, 20]
    assert ID_BATCH_LIMIT == 50
    assert client.deleted_ids == list(resolution.to_delete)
    assert [b.status for b in result.batches] == [SUCCEEDED] * 3


def test_batches_never_exceed_the_limit_and_lose_nothing():
    ids = tuple(f"a{i}" for i in range(137))
    batches = list(batches_of(ids))
    assert [len(b) for b in batches] == [50, 50, 37]
    assert [i for batch in batches for i in batch] == list(ids)


# -- full success ------------------------------------------------------------


def test_a_clean_run_reports_every_album_removed_and_nothing_else(tmp_path):
    resolution = resolution_of(120)
    result = run(resolution, FakeClient(), tmp_path)

    assert result.removed_ids == resolution.to_delete
    assert result.failed_ids == ()
    assert result.never_attempted_ids == ()
    assert result.is_clean
    assert result.interrupted is None


# -- a batch that fails every retry ------------------------------------------


def test_a_batch_that_fails_every_retry_is_failed_and_the_rest_still_proceed(tmp_path):
    """The documented policy: batches are independent, so one failure does not
    abandon the work the reviewer already approved. The failed batch is reported as
    failed -- state unknown -- and every following batch is issued normally."""
    resolution = resolution_of(150)  # three batches
    boom = RuntimeError("Spotify said no")
    # Calls 1-4 are the four attempts at batch 1; 5 and 6 are batches 2 and 3.
    client = FakeClient({1: boom, 2: boom, 3: boom, 4: boom})
    waits: list[float] = []

    result = run(resolution, client, tmp_path, sleep=waits.append)

    assert [b.status for b in result.batches] == [FAILED, SUCCEEDED, SUCCEEDED]
    assert result.batches[0].attempts == 4
    assert "Spotify said no" in result.batches[0].error
    assert result.failed_ids == resolution.to_delete[:50]
    assert result.removed_ids == resolution.to_delete[50:]
    assert result.never_attempted_ids == ()
    assert not result.is_clean
    # Every album appears in exactly one classification, and all of them appear.
    assert set(result.removed_ids) | set(result.failed_ids) == set(resolution.to_delete)
    assert len(client.calls) == 6


def test_a_failed_batch_is_retried_with_increasing_delays(tmp_path):
    boom = RuntimeError("nope")
    client = FakeClient({1: boom, 2: boom, 3: boom, 4: boom})
    waits: list[float] = []

    run(resolution_of(10), client, tmp_path, sleep=waits.append)

    assert len(client.calls) == 4, "four attempts at one batch"
    assert len(waits) == 3, "a wait before each retry, none before the first attempt"
    assert waits == sorted(waits) and waits[0] < waits[-1]
    assert waits[0] >= 1.0 and waits[1] >= 2.0 and waits[2] >= 4.0


def test_a_batch_that_fails_then_succeeds_is_simply_succeeded(tmp_path):
    client = FakeClient({1: RuntimeError("transient")})
    result = run(resolution_of(10), client, tmp_path)

    assert [b.status for b in result.batches] == [SUCCEEDED]
    assert result.batches[0].attempts == 2
    assert result.is_clean


# -- interruption ------------------------------------------------------------


def test_an_interrupt_partway_leaves_the_later_batches_never_attempted(tmp_path):
    """The distinction this module exists for: never-attempted is not failed.

    The batch that was in flight is unknown, so it is failed. The batches after it
    were never issued, so those albums are certainly still in the library, and the
    run must say so rather than lumping them in with the failure.
    """
    resolution = resolution_of(200)  # four batches
    client = FakeClient({2: KeyboardInterrupt()})

    result = run(resolution, client, tmp_path)

    assert [b.status for b in result.batches] == [
        SUCCEEDED,
        FAILED,
        NEVER_ATTEMPTED,
        NEVER_ATTEMPTED,
    ]
    assert result.removed_ids == resolution.to_delete[:50]
    assert result.failed_ids == resolution.to_delete[50:100]
    assert result.never_attempted_ids == resolution.to_delete[100:]
    assert result.interrupted.startswith("KeyboardInterrupt")
    # Nothing was issued after the interrupt.
    assert len(client.calls) == 2
    assert not result.is_clean


def test_an_interrupt_is_never_retried(tmp_path):
    """A retry after Ctrl-C would be the tool overruling the person at the keyboard."""
    client = FakeClient({1: KeyboardInterrupt()})
    result = run(resolution_of(10), client, tmp_path)
    assert len(client.calls) == 1
    assert result.batches[0].attempts == 1


# -- throttling mid-deletion -------------------------------------------------


class _Response:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.content = b""
        self.text = ""

    def json(self) -> dict:
        return {}


class _FakeHttp:
    """Stands in for `requests.Session` inside the real `RateLimitedSession`."""

    def __init__(self, statuses: list[_Response]) -> None:
        self._statuses = statuses
        self.requests: list[tuple[str, str, object]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.requests.append((method, url, json))
        return self._statuses.pop(0)


def test_a_throttling_response_mid_deletion_waits_as_instructed_then_continues(tmp_path):
    """Driven through the real rate-limited session, because the 429 handling that
    matters mid-deletion is the session's, and a fake client would not exercise it."""
    from spotify_manager.infra.client import SpotifyClient
    from spotify_manager.infra.http import RETRY_AFTER_EPSILON, RateLimitedSession

    class _Token:
        def access_token(self) -> str:
            return "token"

    waits: list[float] = []
    session = RateLimitedSession(
        _Token(), requests_per_second=0, sleep=waits.append, monotonic=lambda: 0.0
    )
    # Batch 1 is throttled for 7 seconds, then accepted. Batch 2 is accepted at once.
    session._session = _FakeHttp(
        [
            _Response(429, {"Retry-After": "7"}),
            _Response(200),
            _Response(200),
        ]
    )
    resolution = resolution_of(60)  # two batches

    result = run(resolution, SpotifyClient(session), tmp_path, sleep=lambda s: None)

    assert waits == [7 + RETRY_AFTER_EPSILON], "waited exactly as the API instructed"
    assert [b.status for b in result.batches] == [SUCCEEDED, SUCCEEDED]
    assert result.is_clean
    assert session.stats.throttled == 1
    # The retry is the session's, so the batch itself needed only one attempt.
    assert result.batches[0].attempts == 1
    assert [len(payload["ids"]) for _, _, payload in session._session.requests] == [50, 50, 10]


# -- approving nothing -------------------------------------------------------


def test_approving_nothing_removes_nothing_and_writes_no_restore_file(tmp_path):
    client = FakeClient()
    result = run(Resolution(kept=("a", "b")), client, tmp_path)

    assert client.calls == []
    assert result.batches == ()
    assert result.restore_path is None
    assert result.nothing_requested
    assert result.is_clean  # nothing was requested, so nothing is outstanding
    assert not (tmp_path / "restores").exists()


# -- the undo instruction ----------------------------------------------------


def test_the_undo_command_names_the_restore_file(tmp_path):
    result = run(resolution_of(2), FakeClient(), tmp_path)
    command = restore_command_for(result.restore_path)
    assert command == f"spotify-manager restore {result.restore_path}"


def test_there_is_no_undo_command_when_there_was_no_restore_file():
    assert restore_command_for(None) == ""


# -- progress ----------------------------------------------------------------


def test_progress_names_the_restore_file_before_it_names_a_batch(tmp_path):
    lines: list[str] = []
    run(resolution_of(60), FakeClient(), tmp_path, on_progress=lines.append)
    restore_line = next(i for i, line in enumerate(lines) if "Restore file written" in line)
    first_batch = next(i for i, line in enumerate(lines) if line.startswith("Batch 1"))
    assert restore_line < first_batch


def test_an_empty_result_answers_every_question_without_pretending(tmp_path):
    result = ExecutionResult()
    assert result.removed_count == 0
    assert result.failed_count == 0
    assert result.never_attempted_count == 0
    assert result.nothing_requested
