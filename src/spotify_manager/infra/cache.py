"""The local library snapshot: a plain JSON file with a time-to-live.

No daemon, no Redis -- a single file under the state directory. The snapshot holds
the saved-album listing verbatim as the API returned it, so every later phase
(planning, re-ranking, re-rendering) is a fully offline operation costing zero
requests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a trailing Z as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def snapshot_age_seconds(snapshot: dict[str, Any], now: datetime | None = None) -> float:
    """Seconds elapsed since the snapshot was fetched."""
    now = now or datetime.now(timezone.utc)
    return (now - parse_iso(snapshot["fetched_at"])).total_seconds()


def is_fresh(snapshot: dict[str, Any], ttl_seconds: float, now: datetime | None = None) -> bool:
    """Whether the snapshot is still within its lifetime.

    A snapshot whose `fetched_at` is missing, unparseable, or in the future is
    treated as stale: the failure mode we want is an extra fetch, never operating on
    an unknown-age picture of the library.
    """
    try:
        age = snapshot_age_seconds(snapshot, now)
    except (KeyError, TypeError, ValueError):
        return False
    if age < 0:
        return False
    return age < ttl_seconds


class LibraryCache:
    def __init__(self, path: Path, ttl_seconds: float) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds

    def load(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Return the snapshot if one exists and is still fresh, else None.

        A corrupt file is treated exactly like a missing one -- the cache is a
        convenience and must never be able to fail a run.
        """
        if not self.path.exists():
            return None
        try:
            snapshot = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(snapshot, dict) or "albums" not in snapshot:
            return None
        if not is_fresh(snapshot, self.ttl_seconds, now):
            return None
        return snapshot

    def save(self, albums: list[dict[str, Any]]) -> dict[str, Any]:
        snapshot = {"fetched_at": utcnow_iso(), "albums": albums}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        return snapshot
