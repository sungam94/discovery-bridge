"""Spec §5.1: group MA `media_item_played` events into plays, one open play per player, persisted in open_play."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from bridge.timeutil import iso, parse_iso

IDLE_END = timedelta(minutes=10)
RESTART_MAX_S = 45   # MA reports every ~30 s: a drop to this or below means the item started again
RESTART_DROP_S = 5   # a drop smaller than this is jitter, not a restart


@dataclass(frozen=True)
class Play:
    player_id: str
    uri: str
    media_type: str | None
    userid: str | None
    duration_ms: int | None
    started_at: datetime
    seconds_played: float
    fully_played: bool
    queue_error: bool = False
    source_ma_playlist_id: str | None = None
    source_radio: bool = False
    queue_id: str | None = None


@dataclass(frozen=True)
class ClosedPlay:
    play: Play
    ended_by: str  # "next" | "restart" | "idle"

    @property
    def listened_ms(self) -> int:
        return int(self.play.seconds_played * 1000)


class PlayTracker:
    def __init__(self, conn: sqlite3.Connection, now: Callable[[], datetime]) -> None:
        self._conn, self._now = conn, now

    def _load(self, player_id: str) -> Play | None:
        r = self._conn.execute("SELECT * FROM open_play WHERE player_id = ?", (player_id,)).fetchone()
        if r is None:
            return None
        return Play(r["player_id"], r["ma_item_uri"], r["media_type"], r["userid"], r["duration_ms"],
                    parse_iso(r["started_at"]), r["seconds_played"], bool(r["fully_played"]), bool(r["queue_error"]),
                    r["source_ma_playlist_id"], bool(r["source_radio"]), r["queue_id"])

    def _save(self, p: Play) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO open_play(player_id, ma_item_uri, media_type, userid, duration_ms, started_at, "
            "seconds_played, fully_played, queue_error, source_ma_playlist_id, source_radio, queue_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.player_id, p.uri, p.media_type, p.userid, p.duration_ms, iso(p.started_at), p.seconds_played,
             int(p.fully_played), int(p.queue_error), p.source_ma_playlist_id, int(p.source_radio), p.queue_id,
             iso(self._now())))

    def _close(self, p: Play, ended_by: str) -> ClosedPlay:
        self._conn.execute("DELETE FROM open_play WHERE player_id = ?", (p.player_id,))
        return ClosedPlay(p, ended_by)

    def on_played(self, data: dict) -> tuple[list[ClosedPlay], Play | None]:
        player_id, uri = str(data["player_id"]), str(data["uri"])
        secs = float(data.get("seconds_played") or 0)
        fully = bool(data.get("fully_played"))
        now = self._now()
        closed: list[ClosedPlay] = []
        current = self._load(player_id)
        if current is not None:
            restarted = (current.uri == uri and secs <= RESTART_MAX_S
                         and current.seconds_played - secs >= RESTART_DROP_S)
            if current.uri != uri or restarted:
                closed.append(self._close(current, "restart" if restarted else "next"))
                current = None
        if current is None:
            duration = data.get("duration")
            started = Play(player_id, uri, data.get("media_type"), data.get("userid"),
                           int(duration) * 1000 if duration else None, now - timedelta(seconds=secs), secs, fully)
            self._save(started)
            return closed, started
        self._save(replace(current, seconds_played=secs, fully_played=current.fully_played or fully))
        return closed, None

    def set_source(self, player_id: str, uri: str, source_ma_playlist_id: str | None, source_radio: bool,
                   queue_id: str | None) -> None:
        self._conn.execute("UPDATE open_play SET source_ma_playlist_id = ?, source_radio = ?, queue_id = ? "
                           "WHERE player_id = ? AND ma_item_uri = ?",
                           (source_ma_playlist_id, int(source_radio), queue_id, player_id, uri))

    def sweep(self) -> list[ClosedPlay]:
        limit = iso(self._now() - IDLE_END)
        ids = [r["player_id"] for r in self._conn.execute(
            "SELECT player_id FROM open_play WHERE updated_at < ?", (limit,))]
        return [self._close(p, "idle") for p in (self._load(i) for i in ids) if p is not None]
