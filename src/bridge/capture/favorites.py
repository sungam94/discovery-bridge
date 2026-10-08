"""Spec §5.2 favorites: user-initiated favorite flags only, held 5 min, bursts and sync windows dropped."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from bridge.timeutil import iso, parse_iso

HOLD = timedelta(minutes=5)
BURST_WINDOW = timedelta(seconds=60)
BURST_MAX = 10
SYNC_GRACE = timedelta(minutes=10)


class FavoriteTracker:
    def __init__(self, conn: sqlite3.Connection, now: Callable[[], datetime]) -> None:
        self._conn, self._now = conn, now
        self._favs: set[str] = set()
        self._loaded = False
        self._sync_tasks: dict[str, bool] = {}
        self._sync_ended_at: datetime | None = None
        self._burst_until: datetime | None = None

    def load(self, uris: Iterable[str]) -> None:
        self._favs, self._loaded = set(uris), True

    @property
    def _sync_running(self) -> bool:
        return any(self._sync_tasks.values())

    def _in_sync_window(self) -> bool:
        if self._sync_running:
            return True
        return self._sync_ended_at is not None and self._now() - self._sync_ended_at < SYNC_GRACE

    def on_tasks_updated(self, data) -> None:
        """MA 2.10.4 runs provider syncs as tasks (metadata.task_domain = music_sync) and reports them here."""
        was = self._sync_running
        for t in data if isinstance(data, list) else []:
            if ((t or {}).get("metadata") or {}).get("task_domain") == "music_sync":
                self._sync_tasks[str(t.get("id"))] = t.get("status") in ("pending", "running")
        if was and not self._sync_running:
            self._sync_ended_at = self._now()

    def on_sync_completed(self) -> None:
        self._sync_tasks.clear()
        self._sync_ended_at = self._now()

    def on_item_updated(self, data: dict) -> None:
        if data.get("media_type") != "track" or "favorite" not in data or not data.get("uri"):
            return
        uri, fav = str(data["uri"]), bool(data["favorite"])
        was = uri in self._favs
        (self._favs.add if fav else self._favs.discard)(uri)
        if not self._loaded:
            return
        if fav and not was:
            self._flag(uri)
        elif was and not fav:
            self._conn.execute("DELETE FROM pending_favorite WHERE ma_item_uri = ?", (uri,))

    def _flag(self, uri: str) -> None:
        now = self._now()
        if self._in_sync_window():
            return
        if self._burst_until is not None and now < self._burst_until:
            self._burst_until = now + BURST_WINDOW
            return
        self._conn.execute("INSERT OR REPLACE INTO pending_favorite(ma_item_uri, flagged_at, release_at) "
                           "VALUES (?, ?, ?)", (uri, iso(now), iso(now + HOLD)))
        since = iso(now - BURST_WINDOW)
        recent = self._conn.execute("SELECT count(*) FROM pending_favorite WHERE flagged_at >= ?",
                                    (since,)).fetchone()[0]
        if recent > BURST_MAX:
            self._conn.execute("DELETE FROM pending_favorite WHERE flagged_at >= ?", (since,))
            self._burst_until = now + BURST_WINDOW

    def release_due(self) -> list[tuple[str, datetime]]:
        rows = self._conn.execute("SELECT ma_item_uri, flagged_at FROM pending_favorite WHERE release_at <= ? "
                                  "ORDER BY flagged_at", (iso(self._now()),)).fetchall()
        for r in rows:
            self._conn.execute("DELETE FROM pending_favorite WHERE ma_item_uri = ?", (r["ma_item_uri"],))
        return [(r["ma_item_uri"], parse_iso(r["flagged_at"])) for r in rows]
