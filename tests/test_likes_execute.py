"""Liking, driven against a fake client and a real filesystem.

The batching, retry and classification are `spotify_manager.batching`'s, already
pinned by `test_execute.py` against the deletion path. What is tested here is what
this module adds: that the run record is on disk and complete *before* the first PUT
is issued, that nothing is liked when it cannot be written, and that the result reads
in this feature's vocabulary without losing the three-way classification underneath.

Nothing here touches Spotify. The fake client is a stand-in for the one method `like`
calls, and its job is mostly to fail in specific ways at specific moments.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spotify_manager.batching import FAILED, NEVER_ATTEMPTED, SUCCEEDED
from spotify_manager.likes.execute import like
from spotify_manager.likes.record import LikeRecord, RecordDocumentError, RecordFileError

NOW = datetime(2026, 9, 5, 14, 30, 0, tzinfo=timezone.utc)


class FakeClient:
    """Records what it was asked to like, and fails exactly when told to.

    `fail_on` and `interrupt_on` are *batch* numbers, not call numbers: a batch named
    in `fail_on` fails on every attempt, which is what "exhausts its retries" means.
    """

    def __init__(self, fail_on: set[int] | None = None, interrupt_on: int | None = None,
                 record_file: Path | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on or set()
        self.interrupt_on = interrupt_on
        self.record_file = record_file
        self.record_existed_at_first_call: bool | None = None
        self.batch_number = 0
        self._last: list[str] | None = None

    def save_tracks(self, ids: list[str]) -> None:
        if self.record_file is not None and self.record_existed_at_first_call is None:
            self.record_existed_at_first_call = self.record_file.exists()
        if ids != self._last:
            self.batch_number += 1
            self._last = list(ids)
        self.calls.append(list(ids))
        if self.interrupt_on == self.batch_number:
            raise KeyboardInterrupt()
        if self.batch_number in self.fail_on:
            raise RuntimeError("boom")


def _ids(count: int) -> tuple[str, ...]:
    return tuple(f"t{i}" for i in range(count))


def _only_record(directory: Path) -> Path:
    files = sorted(directory.glob("liked-*.json"))
    assert len(files) == 1, files
    return files[0]


def test_the_run_record_is_on_disk_and_complete_before_the_first_like(tmp_path):
    likes_dir = tmp_path / "likes"
    ids = _ids(120)
    client = FakeClient(record_file=likes_dir / f"liked-{NOW.strftime('%Y-%m-%dT%H-%M-%S')}.json")

    like(ids, client, likes_dir=likes_dir, now=NOW, sleep=lambda _s: None)

    assert client.record_existed_at_first_call is True
    document = json.loads(_only_record(likes_dir).read_text(encoding="utf-8"))
    assert tuple(document["track_ids"]) == ids


def test_the_run_record_is_named_for_the_moment_of_the_run(tmp_path):
    like(_ids(1), FakeClient(), likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)
    assert _only_record(tmp_path).name == "liked-2026-09-05T14-30-00.json"


def test_nothing_is_liked_when_the_run_record_cannot_be_written(tmp_path):
    blocked = tmp_path / "likes"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")
    client = FakeClient()

    with pytest.raises(RecordFileError, match="Nothing was liked"):
        like(_ids(10), client, likes_dir=blocked, now=NOW, sleep=lambda _s: None)

    assert client.calls == []


def test_the_record_lists_what_the_run_set_out_to_do_not_what_worked(tmp_path):
    """A superset is harmless -- unliking an unliked track is a no-op. A subset is not."""
    client = FakeClient(fail_on={2})
    like(_ids(120), client, likes_dir=tmp_path, now=NOW, batch_size=50, sleep=lambda _s: None)

    document = json.loads(_only_record(tmp_path).read_text(encoding="utf-8"))
    assert tuple(document["track_ids"]) == _ids(120)


def test_likes_go_out_in_the_largest_batches_the_api_permits(tmp_path):
    client = FakeClient()
    like(_ids(100), client, likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)
    assert [len(call) for call in client.calls] == [40, 40, 20]
    assert client.batch_number == 3


def test_the_order_the_planner_chose_is_the_order_the_likes_go_out(tmp_path):
    client = FakeClient()
    like(_ids(120), client, likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)
    assert [i for call in client.calls for i in call] == list(_ids(120))


def test_a_clean_run_reports_every_track_liked_and_nothing_else(tmp_path):
    result = like(_ids(60), FakeClient(), likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)

    assert result.is_clean
    assert result.liked_count == 60
    assert result.failed_count == 0
    assert result.never_attempted_count == 0
    assert all(b.status == SUCCEEDED for b in result.batches)


def test_a_batch_that_fails_every_retry_is_failed_and_the_rest_still_proceed(tmp_path):
    client = FakeClient(fail_on={1})
    result = like(_ids(120), client, likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)

    assert result.failed_count == 40
    assert result.liked_count == 80
    assert result.never_attempted_count == 0
    assert [b.status for b in result.batches] == [FAILED, SUCCEEDED, SUCCEEDED]
    assert not result.is_clean


def test_an_interrupt_partway_leaves_the_later_batches_never_attempted(tmp_path):
    client = FakeClient(interrupt_on=2)
    result = like(_ids(120), client, likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)

    assert [b.status for b in result.batches] == [SUCCEEDED, FAILED, NEVER_ATTEMPTED]
    assert result.liked_count == 40
    assert result.failed_count == 40
    assert result.never_attempted_count == 40
    assert result.interrupted is not None


def test_liking_nothing_likes_nothing_and_writes_no_record(tmp_path):
    client = FakeClient()
    result = like((), client, likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)

    assert client.calls == []
    assert list(tmp_path.glob("liked-*.json")) == []
    assert result.record_path is None
    assert result.nothing_requested
    assert result.is_clean


def test_progress_names_the_run_record_before_it_names_a_batch(tmp_path):
    lines: list[str] = []
    like(_ids(60), FakeClient(), likes_dir=tmp_path, now=NOW, sleep=lambda _s: None,
         on_progress=lines.append)

    assert "Run record written" in lines[0]
    assert "Batch 1/2" in lines[1]
    assert "liking" in lines[1] and "tracks" in lines[1]


# -- reading a record back (what an eventual `unlike` will do) ---------------


def test_a_written_record_parses_back_to_the_ids_it_recorded(tmp_path):
    like(_ids(3), FakeClient(), likes_dir=tmp_path, now=NOW, sleep=lambda _s: None)
    raw = json.loads(_only_record(tmp_path).read_text(encoding="utf-8"))
    assert LikeRecord.from_raw(raw).track_ids == _ids(3)


@pytest.mark.parametrize("raw", [None, [], "not a dict", {}, {"track_ids": "t1"},
                                 {"track_ids": [1, 2]}, {"album_ids": ["a1"]}])
def test_a_document_that_is_not_a_run_record_is_refused(raw):
    with pytest.raises(RecordDocumentError):
        LikeRecord.from_raw(raw)


def test_an_empty_record_is_valid_and_means_nothing_to_undo():
    assert LikeRecord.from_raw({"track_ids": []}).track_ids == ()
