"""Spec §3.3 for one slot: fetch, snapshot on change, ISRC lookup for new tracks, resolve."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime

from bridge.hashing import ordered_hash, set_hash
from bridge.models import SpotifyTrack
from bridge.resolve.forward import needs_resolution
from bridge.spotify.client import AuthExpired, RateLimited, SpotifyError
from bridge.timeutil import iso

MAX_CONSECUTIVE_METADATA_FAILURES = 3


def _track_from_row(r: sqlite3.Row) -> SpotifyTrack:
    return SpotifyTrack(id=r["provider_item_id"], name=r["title"], artists=tuple(r["artist"].split("\x1f")),
                        album=r["album"], duration_ms=r["duration_ms"], explicit=bool(r["explicit"]),
                        isrc=r["isrc"])


def latest_tracks(conn: sqlite3.Connection, slot: str) -> list[SpotifyTrack]:
    rows = conn.execute(
        "SELECT s.* FROM playlist_snapshot_item i JOIN source_track s ON s.id = i.source_track_id "
        "WHERE i.snapshot_id = (SELECT max(id) FROM playlist_snapshot WHERE slot = ?) ORDER BY i.position",
        (slot,)).fetchall()
    return [_track_from_row(r) for r in rows]


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, spotify, resolver, now: Callable[[], datetime],
                 request_delay_s: float = 0.5, max_backoff_s: float = 3600,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._conn = conn
        self._spotify = spotify
        self._resolver = resolver
        self._now = now
        self._delay = request_delay_s
        self._max_backoff = max_backoff_s
        self._sleep = sleep
        self.last_problems: list[str] = []

    async def _with_backoff(self, fn, *args):
        """Retry on 429 with exponential backoff; total wait never exceeds max_backoff_s."""
        waited = 0.0
        attempt = 0
        while True:
            try:
                return await fn(*args)
            except RateLimited as exc:
                wait = min(exc.retry_after * (2 ** attempt), self._max_backoff - waited)
                if wait <= 0:
                    raise
                await self._sleep(wait)
                waited += wait
                attempt += 1

    def _upsert_track(self, t: SpotifyTrack) -> int:
        self._conn.execute(
            "INSERT INTO source_track(provider, provider_item_id, artist, title, album, duration_ms, explicit) "
            "VALUES ('spotify', ?, ?, ?, ?, ?, ?) ON CONFLICT(provider, provider_item_id) DO UPDATE SET "
            "artist = excluded.artist, title = excluded.title, album = excluded.album",
            (t.id, "\x1f".join(t.artists), t.name, t.album, t.duration_ms, int(t.explicit)))
        return self._conn.execute("SELECT id FROM source_track WHERE provider='spotify' AND provider_item_id=?",
                                  (t.id,)).fetchone()["id"]

    def accept(self, slot: str, tracks: list[SpotifyTrack]) -> bool:
        from bridge.ingest.guard import GuardState, check_fetch
        row = self._conn.execute("SELECT guard_count, guard_hash, empty_count FROM playlist WHERE slot = ?",
                                 (slot,)).fetchone()
        prev = self._conn.execute(
            "SELECT count(*) AS n FROM playlist_snapshot_item WHERE snapshot_id = "
            "(SELECT max(id) FROM playlist_snapshot WHERE slot = ?)", (slot,)).fetchone()["n"]
        ok, st = check_fetch([t.id for t in tracks], prev or None,
                             GuardState(row["guard_count"], row["guard_hash"], row["empty_count"]))
        self._conn.execute("UPDATE playlist SET guard_count = ?, guard_hash = ?, empty_count = ? WHERE slot = ?",
                           (st.guard_count, st.guard_hash, st.empty_count, slot))
        return ok

    async def _fetch_metadata(self, slot: str) -> None:
        rows = self._conn.execute(
            "SELECT s.provider_item_id FROM playlist_snapshot_item i JOIN source_track s ON s.id = i.source_track_id "
            "WHERE i.snapshot_id = (SELECT max(id) FROM playlist_snapshot WHERE slot = ?) AND s.metadata_fetched = 0",
            (slot,)).fetchall()
        failed = consecutive = 0
        for row in rows:
            tid = row["provider_item_id"]
            try:
                isrc, duration = await self._with_backoff(self._spotify.track_metadata, tid)
            except (AuthExpired, RateLimited):
                raise
            except SpotifyError as exc:
                failed += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_METADATA_FAILURES:
                    self.last_problems.append(f"metadata: stopped after {consecutive} consecutive failures ({exc})")
                    return
                continue  # retried on the next poll
            consecutive = 0
            self._conn.execute("UPDATE source_track SET isrc = ?, duration_ms = COALESCE(?, duration_ms), "
                               "metadata_fetched = 1 WHERE provider='spotify' AND provider_item_id = ?",
                               (isrc, duration, tid))
            await self._sleep(self._delay)
        if failed:
            self.last_problems.append(f"metadata: {failed} track lookups failed")

    async def ingest_slot(self, slot: str, spotify_id: str) -> str:
        self.last_problems = []
        fetched = await self._with_backoff(self._spotify.fetch_playlist, spotify_id)
        tracks = fetched.tracks
        now = self._now()
        self._conn.execute("UPDATE playlist SET last_checked = ? WHERE slot = ?", (iso(now), slot))
        if not self.accept(slot, tracks):
            return "empty" if not tracks else "held"
        ids = [t.id for t in tracks]
        oh = ordered_hash(ids)
        last = self._conn.execute("SELECT ordered_hash FROM playlist_snapshot WHERE slot = ? ORDER BY id DESC LIMIT 1",
                                  (slot,)).fetchone()
        outcome = "unchanged"
        if last is None or last["ordered_hash"] != oh:
            self._conn.execute("BEGIN")
            try:
                sid = self._conn.execute(
                    "INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash, revision_id) "
                    "VALUES (?,?,?,?,?,?)", (slot, spotify_id, iso(now), oh, set_hash(ids), fetched.revision_id)).lastrowid
                for pos, t in enumerate(tracks):
                    self._conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)",
                                       (sid, pos, self._upsert_track(t)))
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            outcome = "snapshot"
        await self._fetch_metadata(slot)
        fetched_ids = {r["provider_item_id"] for r in self._conn.execute(
            "SELECT provider_item_id FROM source_track WHERE provider='spotify' AND metadata_fetched = 1")}
        pending = 0
        for t in latest_tracks(self._conn, slot):
            if (t.isrc or t.id in fetched_ids) and needs_resolution(self._conn, t.id, self._now()):
                res = await self._resolver.resolve(t)
                if res.status == "pending":
                    pending += 1
                await self._sleep(self._delay)
        if pending:
            self.last_problems.append(f"{pending} resolutions pending (ma_error)")
        return outcome
