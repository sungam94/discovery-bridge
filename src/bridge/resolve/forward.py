"""Spec §4.4 Spotify -> TIDAL. Unplayable-item invalidation (queue errors) is Phase 2."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from websockets.exceptions import WebSocketException

from bridge.ma.client import MaError
from bridge.models import SpotifyTrack
from bridge.resolve.match import candidates_from_search, choose
from bridge.timeutil import iso, parse_iso

_DELAYS = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)]
TRANSIENT = (MaError, OSError, TimeoutError, WebSocketException)


def next_retry(retry_count: int, now: datetime) -> datetime:
    return now + (_DELAYS[retry_count] if retry_count < len(_DELAYS) else timedelta(days=7))


@dataclass(frozen=True)
class Resolution:
    status: str
    ma_uri: str | None = None
    method: str | None = None
    reason: str | None = None


def _mapping(conn: sqlite3.Connection, spotify_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM track_mapping WHERE direction='forward' AND spotify_id=? AND status!='invalidated'",
        (spotify_id,)).fetchone()


def needs_resolution(conn: sqlite3.Connection, spotify_id: str, now: datetime) -> bool:
    row = _mapping(conn, spotify_id)
    if row is None or row["status"] == "pending":
        return True
    if row["status"] == "unmatched":
        return row["unmatched_retry_at"] is None or parse_iso(row["unmatched_retry_at"]) <= now
    return False


class ForwardResolver:
    def __init__(self, conn: sqlite3.Connection, ma, now: Callable[[], datetime]) -> None:
        self._conn = conn
        self._ma = ma
        self._now = now

    def _save(self, track: SpotifyTrack, status: str, *, uri: str | None = None, method: str | None = None,
              reason: str | None = None) -> Resolution:
        now = self._now()
        prev = _mapping(self._conn, track.id)
        retry_count = prev["retry_count"] if prev else 0
        retry_at = None
        if status == "unmatched":
            retry_at = iso(next_retry(retry_count, now))
            retry_count += 1
        self._conn.execute(
            "DELETE FROM track_mapping WHERE direction='forward' AND spotify_id=? AND status!='invalidated'",
            (track.id,))
        self._conn.execute(
            "INSERT INTO track_mapping(direction, spotify_id, isrc, ma_provider_uri, method, status, "
            "unmatched_reason, unmatched_retry_at, retry_count, resolved_at) VALUES ('forward',?,?,?,?,?,?,?,?,?)",
            (track.id, track.isrc, uri, method, status, reason, retry_at, retry_count, iso(now)))
        return Resolution(status=status, ma_uri=uri, method=method, reason=reason)

    async def resolve(self, track: SpotifyTrack) -> Resolution:
        override = self._conn.execute(
            "SELECT * FROM manual_override WHERE direction='forward' AND spotify_id=?", (track.id,)).fetchone()
        if override is not None:
            if override["blocked"]:
                return self._save(track, "unmatched", reason="blocked")
            return self._save(track, "matched", uri=override["ma_provider_uri"], method="override")

        row = _mapping(self._conn, track.id)
        if row is not None and row["status"] == "matched":
            return Resolution("matched", row["ma_provider_uri"], row["method"])
        if row is not None and not needs_resolution(self._conn, track.id, self._now()):
            return Resolution("unmatched", reason=row["unmatched_reason"])

        if not track.isrc:
            return self._save(track, "unmatched", reason="no_isrc")
        if track.duration_ms <= 0:
            return self._save(track, "unmatched", reason="no_duration")
        artist = track.artists[0] if track.artists else ""
        queries = list(dict.fromkeys([f"{artist} {track.name}".strip(), f"{artist} {track.name} {track.album}".strip()]))
        excluded = {r["ma_provider_uri"] for r in self._conn.execute(
            "SELECT ma_provider_uri FROM excluded_item WHERE spotify_id=?", (track.id,))}
        cands = []
        try:
            for q in queries:
                cands = [c for c in candidates_from_search(await self._ma.search_tracks(q))
                         if track.isrc in c.isrcs and c.uri not in excluded]
                if cands:
                    break
        except TRANSIENT:
            return self._save(track, "pending", reason="ma_error")
        if not cands:
            return self._save(track, "unmatched", reason="no_match")
        chosen = choose(track, cands)
        if chosen is None:
            return self._save(track, "unmatched", reason="duration_mismatch")
        try:
            await self._ma.get_item(chosen.uri)
        except TRANSIENT:
            return self._save(track, "pending", reason="ma_error")
        return self._save(track, "matched", uri=chosen.uri,
                          method="library_isrc" if chosen.from_library else "catalog_isrc")
