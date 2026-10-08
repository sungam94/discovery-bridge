"""Spec §5.3: policy filters, then one taste_event per play or favorite."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from bridge.capture.outcome import JOB_KIND, classify
from bridge.capture.plays import ClosedPlay
from bridge.capture.source import bridge_source
from bridge.config import Thresholds
from bridge.repo import is_paused
from bridge.resolve.reverse import TRANSIENT, canonical_uri, split_uri
from bridge.timeutil import iso

PLAYERS_TTL = timedelta(minutes=10)


class PlayerDirectory:
    """Allowlist check (§5.3 filter 1). A group counts only if every member is on the allowlist."""

    def __init__(self, ma, allowlist: frozenset[str], now: Callable[[], datetime]) -> None:
        self._ma, self._allow, self._now = ma, allowlist, now
        self._players: dict[str, dict] = {}
        self._loaded_at: datetime | None = None

    async def _refresh(self) -> bool:
        try:
            self._players = {p["player_id"]: p for p in await self._ma.call("players/all")}
            self._loaded_at = self._now()
            return True
        except TRANSIENT:
            return False

    async def check(self, player_id: str) -> str | None:
        stale = self._loaded_at is None or self._now() - self._loaded_at > PLAYERS_TTL
        ok = await self._refresh() if stale or player_id not in self._players else True
        player = self._players.get(player_id)
        if player is None:
            if not ok:  # MA unreachable: no group information, decide on the ID alone
                return None if player_id in self._allow else "player_not_allowed"
            return "player_unknown"
        members = set(player.get("group_members") or []) | {player_id}
        return None if members <= self._allow else "player_not_allowed"


def forward_spotify_id(conn: sqlite3.Connection, uri: str, snapshot_id: int | None) -> tuple[str, str | None] | None:
    """Several Spotify IDs may map to one TIDAL item (§4.5 ambiguity): prefer the source snapshot, then the newest."""
    row = conn.execute(
        "SELECT m.spotify_id, m.isrc, EXISTS(SELECT 1 FROM playlist_snapshot_item i JOIN source_track s "
        "  ON s.id = i.source_track_id WHERE i.snapshot_id = ? AND s.provider_item_id = m.spotify_id) AS in_snap "
        "FROM track_mapping m WHERE m.direction = 'forward' AND m.status = 'matched' AND m.ma_provider_uri = ? "
        "ORDER BY in_snap DESC, m.resolved_at DESC, m.id DESC LIMIT 1", (snapshot_id, uri)).fetchone()
    return (row["spotify_id"], row["isrc"]) if row is not None else None


class Recorder:
    def __init__(self, conn: sqlite3.Connection, ma, players, reverse, thresholds: Thresholds,
                 now: Callable[[], datetime]) -> None:
        self._conn, self._ma, self._players, self._reverse = conn, ma, players, reverse
        self._t, self._now = thresholds, now

    async def _key(self, uri: str) -> str:
        try:
            return (await canonical_uri(self._ma, uri))[0]
        except TRANSIENT:
            return uri

    async def _spotify(self, key: str, snapshot_id: int | None, wants_job: bool) -> tuple[str | None, str | None, str | None]:
        """Returns (spotify_id, isrc, failing policy result or None)."""
        fwd = forward_spotify_id(self._conn, key, snapshot_id)
        if fwd is not None:
            return fwd[0], fwd[1], None
        if not wants_job:
            return None, None, None
        res = await self._reverse.resolve(key)
        if res.status == "matched":
            return res.spotify_id, None, None
        return None, None, "pending_resolve" if res.status == "pending" else "unresolved"

    def _final(self) -> str:
        return "feedback_paused" if is_paused(self._conn, "feedback_paused") else "eligible"

    def _insert(self, **f) -> None:
        cols = ", ".join(f)
        self._conn.execute(f"INSERT OR IGNORE INTO taste_event({cols}) VALUES ({', '.join('?' * len(f))})",
                           tuple(f.values()))

    async def record_play(self, c: ClosedPlay) -> str:
        p = c.play
        outcome, policy = classify(c.listened_ms, p.duration_ms, p.fully_played, p.queue_error, c.ended_by, self._t)
        src = bridge_source(self._conn, p.source_ma_playlist_id) if p.source_ma_playlist_id else None
        if policy is None:
            policy = await self._players.check(p.player_id)
        if policy is None and p.media_type not in (None, "track"):
            policy = "not_a_track"
        if policy is None and p.source_radio:
            policy = "radio"
        if policy is None and outcome not in JOB_KIND:
            policy = "no_job"
        key = await self._key(p.uri)
        spotify_id, isrc, failed = await self._spotify(key, src[1] if src else None, wants_job=policy is None)
        policy = policy or failed or self._final()
        provider, item_id = split_uri(key)
        self._insert(source="ma_play", ma_item_uri=p.uri, provider=provider, provider_item_id=item_id, isrc=isrc,
                     queue_id=p.queue_id, player_ids=p.player_id, userid=p.userid, started_at=iso(p.started_at),
                     listened_ms=c.listened_ms, duration_ms=p.duration_ms, outcome=outcome, spotify_id=spotify_id,
                     source_ma_playlist_id=p.source_ma_playlist_id, source_spotify_playlist_id=src[0] if src else None,
                     source_snapshot_id=src[1] if src else None, source_radio=int(p.source_radio),
                     policy_result=policy)
        return policy

    async def record_favorite(self, uri: str, flagged_at: datetime) -> str:
        key = await self._key(uri)
        spotify_id, isrc, failed = await self._spotify(key, None, wants_job=True)
        policy = failed or self._final()
        provider, item_id = split_uri(key)
        self._insert(source="ma_favorite", ma_item_uri=uri, provider=provider, provider_item_id=item_id, isrc=isrc,
                     started_at=iso(flagged_at), outcome="favorite", spotify_id=spotify_id, policy_result=policy)
        return policy

    async def retry_pending(self, max_age: timedelta = timedelta(hours=48)) -> int:
        """Pending events, and unresolved ones whose reverse retry time has come (the resolver gates on it)."""
        rows = self._conn.execute("SELECT id, provider, provider_item_id, policy_result FROM taste_event "
                                  "WHERE policy_result IN ('pending_resolve', 'unresolved') AND started_at >= ? "
                                  "ORDER BY id", (iso(self._now() - max_age),)).fetchall()
        changed = 0
        for r in rows:
            res = await self._reverse.resolve(f"{r['provider']}://track/{r['provider_item_id']}")
            if res.status == "pending":
                break  # Spotify or MA is down: every further row would fail the same way
            policy = self._final() if res.status == "matched" else "unresolved"
            if policy == r["policy_result"]:
                continue
            self._conn.execute("UPDATE taste_event SET spotify_id = ?, policy_result = ? WHERE id = ?",
                               (res.spotify_id, policy, r["id"]))
            changed += 1
        return changed
