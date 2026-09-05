"""The `like-album-tracks` shell: confirmation, dry runs, snapshot hygiene, results.

`run` is exercised with the client, the loaders and `likes.execute.like` monkeypatched
out, so the claims that matter -- that a non-terminal stdin cannot approve a run, that
a dry run writes nothing, and that the Liked Songs snapshot is gone afterwards however
the run ended -- are checked without a network. `confirmed` and `format_results` are
exercised directly against plain values, as `test_dedupe_cmd.py` does.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from spotify_manager.batching import FAILED, NEVER_ATTEMPTED, SUCCEEDED, BatchOutcome
from spotify_manager.commands import like_cmd
from spotify_manager.config import Config
from spotify_manager.likes.execute import LikeResult


def _config(tmp_path: Path) -> Config:
    return Config(
        client_id="id",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        state_dir=tmp_path / "state",
    )


class _Tty(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# -- the confirmation --------------------------------------------------------


def test_yes_at_the_prompt_approves():
    assert like_cmd.confirmed(10, assume_yes=False, stream=_Tty("yes\n")) is True


@pytest.mark.parametrize("answer", ["no\n", "\n", "y\n", "YES please\n", ""])
def test_anything_but_yes_does_not_approve(answer):
    assert like_cmd.confirmed(10, assume_yes=False, stream=_Tty(answer)) is False


def test_confirmation_is_case_insensitive_and_ignores_surrounding_space():
    assert like_cmd.confirmed(10, assume_yes=False, stream=_Tty("  YES  \n")) is True


def test_the_yes_flag_approves_without_asking():
    assert like_cmd.confirmed(10, assume_yes=True, stream=_Tty("", tty=False)) is True


def test_a_non_terminal_stdin_refuses_rather_than_proceeding(capsys):
    """A redirect must never be able to approve a ten-thousand-row mutation."""
    assert like_cmd.confirmed(9473, assume_yes=False, stream=_Tty("", tty=False)) is False
    assert "--yes" in capsys.readouterr().err


def test_a_non_terminal_stdin_refuses_rather_than_reading():
    """It must not hang either: a CI job cannot answer a prompt."""

    class _NeverRead(_Tty):
        def readline(self, *_args):  # pragma: no cover - never called, that is the claim
            raise AssertionError("the prompt read a stdin that is not a terminal")

    assert like_cmd.confirmed(1, assume_yes=False, stream=_NeverRead("", tty=False)) is False


def test_a_yes_on_a_pipe_is_still_not_enough_without_the_flag():
    """The answer being there does not make it an answer we asked for."""
    assert like_cmd.confirmed(1, assume_yes=False, stream=_Tty("yes\n", tty=False)) is False


# -- the run -----------------------------------------------------------------


class _StubClient:
    def __init__(self):
        self.saved: list[list[str]] = []

    def save_tracks(self, ids):  # pragma: no cover - only reached if wiring is wrong
        self.saved.append(list(ids))


def _snapshot(tmp_path, monkeypatch, *, liked=(), like_impl=None):
    """Wire `run` to two in-memory snapshots and an optional fake `like`."""
    config = _config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    liked_snapshot = {
        "fetched_at": "now",
        "tracks": [{"track": {"id": i}} for i in liked],
    }
    config.liked_tracks_snapshot_path.write_text(json.dumps(liked_snapshot), encoding="utf-8")

    album_snapshot = {
        "fetched_at": "now",
        "albums": [
            {
                "added_at": "2020-01-01T00:00:00Z",
                "album": {
                    "id": "al1",
                    "name": "Record",
                    "album_type": "album",
                    "release_date": "2020-01-01",
                    "release_date_precision": "day",
                    "total_tracks": 2,
                    "artists": [{"id": "ar1", "name": "The Band"}],
                    "tracks": {
                        "total": 2,
                        "items": [
                            {"id": "t1", "name": "A", "duration_ms": 1000, "disc_number": 1,
                             "track_number": 1, "artists": [{"name": "The Band"}]},
                            {"id": "t2", "name": "B", "duration_ms": 2000, "disc_number": 1,
                             "track_number": 2, "artists": [{"name": "The Band"}]},
                        ],
                    },
                },
            }
        ],
    }

    class _Loaded:
        def __init__(self, snapshot):
            self.snapshot = snapshot
            self.from_cache = True
            self.pages = 0
            self.requests = 0
            self.elapsed_seconds = 0.0
            self.age_seconds = 0.0

    monkeypatch.setattr(like_cmd, "load_config", lambda: config)
    monkeypatch.setattr(like_cmd, "build_client", lambda *a, **k: _StubClient())
    monkeypatch.setattr(like_cmd, "load_library", lambda *a, **k: _Loaded(album_snapshot))
    monkeypatch.setattr(
        like_cmd,
        "load_liked_tracks",
        lambda *a, **k: _Loaded(json.loads(config.liked_tracks_snapshot_path.read_text())),
    )
    monkeypatch.setattr(like_cmd, "complete_album_tracks", lambda library, client, **k: library)
    if like_impl is not None:
        monkeypatch.setattr(like_cmd, "like", like_impl)
    return config


def _args(**overrides):
    values = dict(refresh=False, yes=True, dry_run=False, rate=None, verbose=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_clean_run_likes_the_planned_tracks_and_exits_zero(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_like(track_ids, client, **kwargs):
        seen["ids"] = track_ids
        seen["likes_dir"] = kwargs["likes_dir"]
        return LikeResult(
            batches=(BatchOutcome(number=1, ids=track_ids, status=SUCCEEDED),),
            requested_ids=track_ids,
            record_path=Path("liked-x.json"),
        )

    config = _snapshot(tmp_path, monkeypatch, like_impl=fake_like)
    assert like_cmd.run(_args()) == 0
    assert seen["ids"] == ("t1", "t2")
    assert seen["likes_dir"] == config.likes_dir
    assert "Every track was liked." in capsys.readouterr().out


def test_a_run_that_did_not_finish_exits_non_zero(tmp_path, monkeypatch, capsys):
    def fake_like(track_ids, client, **kwargs):
        return LikeResult(
            batches=(BatchOutcome(number=1, ids=track_ids, status=FAILED, error="boom"),),
            requested_ids=track_ids,
        )

    _snapshot(tmp_path, monkeypatch, like_impl=fake_like)
    assert like_cmd.run(_args()) == 1
    assert "THIS RUN DID NOT FINISH" in capsys.readouterr().out


def test_a_dry_run_writes_nothing_and_issues_nothing(tmp_path, monkeypatch, capsys):
    def never(*_a, **_k):  # pragma: no cover - the point is that it is never called
        raise AssertionError("a dry run reached the executor")

    config = _snapshot(tmp_path, monkeypatch, like_impl=never)
    assert like_cmd.run(_args(dry_run=True)) == 0

    assert not config.likes_dir.exists()
    out = capsys.readouterr().out
    assert "TO LIKE: 2 tracks." in out
    assert "no run record was written" in out


def test_a_dry_run_leaves_the_liked_songs_snapshot_alone(tmp_path, monkeypatch):
    """It changed nothing, so the cache it read is still true."""
    config = _snapshot(tmp_path, monkeypatch, like_impl=lambda *a, **k: None)
    like_cmd.run(_args(dry_run=True))
    assert config.liked_tracks_snapshot_path.exists()


def test_refusing_at_the_prompt_likes_nothing_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    def never(*_a, **_k):  # pragma: no cover
        raise AssertionError("a refused run reached the executor")

    config = _snapshot(tmp_path, monkeypatch, like_impl=never)
    monkeypatch.setattr(like_cmd, "confirmed", lambda *a, **k: False)

    assert like_cmd.run(_args(yes=False)) == 1
    assert "Nothing was liked." in capsys.readouterr().out
    assert config.liked_tracks_snapshot_path.exists()


def test_the_liked_songs_snapshot_is_discarded_after_a_run(tmp_path, monkeypatch):
    """The run changed Liked Songs, so the snapshot it read is now wrong."""
    def fake_like(track_ids, client, **kwargs):
        return LikeResult(
            batches=(BatchOutcome(number=1, ids=track_ids, status=SUCCEEDED),),
            requested_ids=track_ids,
        )

    config = _snapshot(tmp_path, monkeypatch, like_impl=fake_like)
    like_cmd.run(_args())
    assert not config.liked_tracks_snapshot_path.exists()


def test_the_liked_songs_snapshot_is_discarded_even_when_the_run_is_interrupted(
    tmp_path, monkeypatch
):
    """Especially then: the next run must refetch to know what is left to do."""
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt()

    config = _snapshot(tmp_path, monkeypatch, like_impl=interrupted)
    with pytest.raises(KeyboardInterrupt):
        like_cmd.run(_args())
    assert not config.liked_tracks_snapshot_path.exists()


def test_a_library_that_is_entirely_liked_already_does_nothing_at_all(tmp_path, monkeypatch, capsys):
    def never(*_a, **_k):  # pragma: no cover
        raise AssertionError("there was nothing to like")

    config = _snapshot(tmp_path, monkeypatch, liked=("t1", "t2"), like_impl=never)
    assert like_cmd.run(_args()) == 0
    assert "Nothing to do." in capsys.readouterr().out
    assert config.liked_tracks_snapshot_path.exists()


# -- the results block -------------------------------------------------------


def test_format_results_reports_a_clean_run():
    result = LikeResult(
        batches=(BatchOutcome(number=1, ids=("t1", "t2"), status=SUCCEEDED, attempts=1),),
        requested_ids=("t1", "t2"),
        record_path=Path("/state/likes/liked-x.json"),
    )
    text = like_cmd.format_results(result)
    assert "requested to like      2" in text
    assert "liked                  2" in text
    assert "Every track was liked." in text
    assert "/state/likes/liked-x.json" in text


def test_format_results_distinguishes_never_attempted_from_failed():
    result = LikeResult(
        batches=(
            BatchOutcome(number=1, ids=("f1",), status=FAILED, attempts=4, error="boom"),
            BatchOutcome(number=2, ids=("n1",), status=NEVER_ATTEMPTED),
        ),
        requested_ids=("f1", "n1"),
    )
    text = like_cmd.format_results(result)
    assert "may or may not be liked" in text
    assert "they were not liked" in text
    assert "Batch 1 error: boom" in text
    assert "Re-run the command to finish" in text


def test_format_results_does_not_print_ten_thousand_ids():
    ids = tuple(f"t{i}" for i in range(500))
    result = LikeResult(
        batches=(BatchOutcome(number=1, ids=ids, status=NEVER_ATTEMPTED),),
        requested_ids=ids,
    )
    text = like_cmd.format_results(result)
    assert "... and 450 more" in text


# -- completing a truncated album's track list -------------------------------


class _TrackClient:
    """Answers `fetch_album_tracks`, or fails for a named album."""

    def __init__(self, tracks=None, fail_for=()):
        self.tracks = tracks or {}
        self.fail_for = set(fail_for)
        self.asked: list[str] = []

    def fetch_album_tracks(self, album_id):
        self.asked.append(album_id)
        if album_id in self.fail_for:
            raise RuntimeError("boom")
        return self.tracks.get(album_id, [])


def _entry(album_id, track_ids, *, truncated):
    from spotify_manager.dedupe.models import Artist, SavedAlbum
    from spotify_manager.likes.models import AlbumTracks, Track

    return AlbumTracks(
        album=SavedAlbum(
            id=album_id,
            name=album_id,
            artists=(Artist(id="ar1", name="The Band"),),
            album_type="album",
            release_date="2020-01-01",
            release_date_precision="day",
            total_tracks=60,
            added_at="2020-01-01T00:00:00Z",
        ),
        tracks=tuple(Track(id=i, name=i, artist_names=("The Band",), duration_ms=1000) for i in track_ids),
        truncated=truncated,
    )


def _raw_track(track_id):
    return {"id": track_id, "name": track_id, "duration_ms": 1000, "artists": [{"name": "The Band"}]}


def test_only_a_truncated_album_costs_a_request():
    from spotify_manager.commands.library import complete_album_tracks
    from spotify_manager.likes.models import TrackLibrary

    client = _TrackClient(tracks={"big": [_raw_track("t1"), _raw_track("t2")]})
    library = TrackLibrary("now", (_entry("small", ("s1",), truncated=False),
                                   _entry("big", ("t1",), truncated=True)))

    completed = complete_album_tracks(library, client)

    assert client.asked == ["big"]
    assert [t.id for t in completed.albums[1].tracks] == ["t1", "t2"]
    assert completed.albums[1].truncated is False


def test_an_album_whose_track_list_cannot_be_fetched_does_not_end_the_run():
    from spotify_manager.commands.library import complete_album_tracks
    from spotify_manager.likes.models import TrackLibrary

    client = _TrackClient(fail_for={"broken"})
    library = TrackLibrary("now", (_entry("broken", ("t1",), truncated=True),
                                   _entry("fine", ("f1",), truncated=False)))

    completed = complete_album_tracks(library, client)

    assert [t.id for t in completed.albums[0].tracks] == ["t1"]
    assert completed.albums[0].truncated is True, "still incomplete, and reported as such"
    assert [t.id for t in completed.albums[1].tracks] == ["f1"]
