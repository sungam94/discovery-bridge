"""Spec §4.7: publish current playlists when the resolved item set changes."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from bridge.hashing import items_hash
from bridge.timeutil import iso, parse_iso

_HISTORY_PREFIX = {"discover_weekly": "DW", "release_radar": "RR"}
_FIXED = {"discover_weekly": "Discover Weekly", "release_radar": "Release Radar", "daylist": "daylist"}


def playlist_name(slot: str) -> str:
    if slot.startswith("daily_mix_"):
        return f"Spotify · Daily Mix {slot.rsplit('_', 1)[1]}"
    return f"Spotify · {_FIXED[slot]}"


def history_base_name(slot: str, fetched_at_iso: str, tz: str) -> str:
    local_date = parse_iso(fetched_at_iso).astimezone(ZoneInfo(tz)).date().isoformat()
    return f"Spotify · {_HISTORY_PREFIX[slot]} {local_date}"


def ensure_playlist_row(conn: sqlite3.Connection, slot: str, kind: str, spotify_id: str | None,
                        name: str | None = None) -> None:
    """The name is set once: a later rename on Spotify must not create a second MA playlist."""
    conn.execute("INSERT INTO playlist(slot, kind, spotify_id, name) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(slot) DO UPDATE SET spotify_id = COALESCE(excluded.spotify_id, playlist.spotify_id)",
                 (slot, kind, spotify_id, name or playlist_name(slot)))


def snapshot_uris(conn: sqlite3.Connection, snapshot_id: int) -> tuple[list[str], int, int, int]:
    rows = conn.execute(
        "SELECT i.position, m.status, m.ma_provider_uri FROM playlist_snapshot_item i "
        "JOIN source_track s ON s.id = i.source_track_id "
        "LEFT JOIN track_mapping m ON m.direction='forward' AND m.spotify_id = s.provider_item_id "
        "  AND m.status != 'invalidated' "
        "WHERE i.snapshot_id = ? ORDER BY i.position", (snapshot_id,)).fetchall()
    uris: list[str] = []
    matched = pending = 0
    for r in rows:
        if r["status"] == "matched" and r["ma_provider_uri"]:
            matched += 1
            if r["ma_provider_uri"] not in uris:
                uris.append(r["ma_provider_uri"])
        elif r["status"] in (None, "pending"):
            pending += 1
    return uris, matched, pending, len(rows)


class PublishIncomplete(Exception):
    """MA kept too few of the published items; nothing is recorded, so the next poll retries."""


class Publisher:
    def __init__(self, conn: sqlite3.Connection, ma, now: Callable[[], datetime],
                 pending_wait: timedelta = timedelta(hours=6), publish_history: frozenset[str] = frozenset(),
                 *, tz: str) -> None:
        self._conn = conn
        self._ma = ma
        self._now = now
        self._wait = pending_wait
        self._history = publish_history
        self._tz = tz

    def _update(self, slot: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE playlist SET {cols} WHERE slot = ?", (*fields.values(), slot))

    def _row(self, slot: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM playlist WHERE slot = ?", (slot,)).fetchone()

    async def _ensure_ma_playlist(self, slot: str, row: sqlite3.Row) -> str:
        pid = row["ma_playlist_id"]
        if pid and await self._ma.get_playlist(pid) is not None:
            return pid
        pid = await self._ma.find_playlist_by_name(row["name"]) or await self._ma.create_playlist(row["name"])
        self._update(slot, ma_playlist_id=pid, published_items_hash=None)
        return pid

    def _pending_blocks(self, slot: str, row: sqlite3.Row, snapshot_id: int) -> bool:
        """True while the 6 h wait for this snapshot's pending tracks is still running."""
        if row["publish_blocked_snapshot_id"] != snapshot_id or row["publish_blocked_since"] is None:
            self._update(slot, publish_blocked_since=iso(self._now()), publish_blocked_snapshot_id=snapshot_id)
            return True
        return self._now() - parse_iso(row["publish_blocked_since"]) < self._wait

    async def _after_publish(self, slot: str, snap: sqlite3.Row, uris: list[str]) -> None:
        """Spec §4.7 dated history: once per new track set, after the pending wait (check() calls this only then)."""
        row = self._row(slot)
        if slot not in self._history or slot not in _HISTORY_PREFIX or row["history_set_hash"] == snap["set_hash"]:
            return
        name = snap["ma_history_name"]
        if not name:
            base = history_base_name(slot, snap["fetched_at"], self._tz)
            taken = {r["ma_history_name"] for r in self._conn.execute(
                "SELECT ma_history_name FROM playlist_snapshot WHERE ma_history_name IS NOT NULL")}
            name, n = base, 2
            while name in taken or await self._ma.find_playlist_by_name(name) is not None:
                name, n = f"{base}-{n}", n + 1
            self._conn.execute("UPDATE playlist_snapshot SET ma_history_name = ? WHERE id = ?", (name, snap["id"]))
        pid = await self._ma.find_playlist_by_name(name) or await self._ma.create_playlist(name)
        await self._ma.replace_playlist(pid, uris)
        self._conn.execute("UPDATE playlist_snapshot SET ma_history_playlist_id = ? WHERE id = ?", (pid, snap["id"]))
        self._update(slot, history_set_hash=snap["set_hash"])

    async def check(self, slot: str) -> str:
        row = self._row(slot)
        snap = self._conn.execute("SELECT * FROM playlist_snapshot WHERE slot = ? ORDER BY id DESC LIMIT 1",
                                  (slot,)).fetchone()
        if row is None or snap is None:
            return "no_snapshot"
        uris, matched, pending, total = snapshot_uris(self._conn, snap["id"])
        self._conn.execute("UPDATE playlist_snapshot SET coverage_matched = ?, coverage_total = ? WHERE id = ?",
                           (matched, total, snap["id"]))
        if not uris:
            return "no_matches"
        if pending:
            if self._pending_blocks(slot, row, snap["id"]):
                return "waiting"
        else:
            self._update(slot, publish_blocked_since=None, publish_blocked_snapshot_id=None)
        pid = await self._ensure_ma_playlist(slot, self._row(slot))
        h = items_hash(uris)
        if h == self._row(slot)["published_items_hash"]:
            await self._after_publish(slot, snap, uris)
            return "unchanged"
        missing = await self._ma.replace_playlist(pid, uris)
        if len(missing) * 2 > len(uris):
            raise PublishIncomplete(f"MA kept too few items: {len(missing)} of {len(uris)} missing")
        self._update(slot, published_items_hash=h, published_snapshot_id=snap["id"])
        await self._after_publish(slot, snap, uris)
        return f"published ({len(missing)} missing)" if missing else "published"
