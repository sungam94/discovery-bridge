"""Spec §4.5: MA item -> Spotify ID. Key: the provider URI that played the item (TIDAL for library items)."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from websockets.exceptions import WebSocketException

from bridge.ma.client import MaError, tidal_uri
from bridge.resolve.forward import next_retry
from bridge.resolve.match import choose_spotify
from bridge.spotify.client import SpotifyError
from bridge.timeutil import iso, parse_iso

TRANSIENT = (MaError, OSError, TimeoutError, WebSocketException, SpotifyError)


def split_uri(uri: str) -> tuple[str, str]:
    return uri.split("://", 1)[0], uri.rsplit("/", 1)[1]


async def canonical_uri(ma, uri: str) -> tuple[str, dict | None]:
    if not uri.startswith("library://"):
        return uri, None
    item = await ma.get_item(uri)
    return tidal_uri(item), item


@dataclass(frozen=True)
class ReverseResult:
    status: str  # matched | unmatched | pending
    spotify_id: str | None = None
    reason: str | None = None


class ReverseResolver:
    def __init__(self, conn: sqlite3.Connection, ma, spotify, now: Callable[[], datetime]) -> None:
        self._conn, self._ma, self._spotify, self._now = conn, ma, spotify, now

    def _mapping(self, key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM track_mapping WHERE direction = 'reverse' "
                                  "AND ma_provider_uri = ? AND status != 'invalidated'", (key,)).fetchone()

    def _save(self, key: str, library_uri: str | None, isrc: str | None, status: str,
              spotify_id: str | None = None, reason: str | None = None) -> ReverseResult:
        now = self._now()
        prev = self._mapping(key)
        retry_count = prev["retry_count"] if prev else 0
        retry_at = iso(next_retry(retry_count, now)) if status == "unmatched" else None
        self._conn.execute("DELETE FROM track_mapping WHERE direction = 'reverse' AND ma_provider_uri = ? "
                           "AND status != 'invalidated'", (key,))
        self._conn.execute(
            "INSERT INTO track_mapping(direction, spotify_id, isrc, ma_provider_uri, ma_library_uri, method, status, "
            "unmatched_reason, unmatched_retry_at, retry_count, resolved_at) VALUES ('reverse',?,?,?,?,?,?,?,?,?,?)",
            (spotify_id, isrc, key, library_uri, "reverse_isrc" if status == "matched" else None, status, reason,
             retry_at, retry_count + (1 if status == "unmatched" else 0), iso(now)))
        return ReverseResult(status, spotify_id, reason)

    async def resolve(self, ma_uri: str) -> ReverseResult:
        try:
            key, item = await canonical_uri(self._ma, ma_uri)
        except TRANSIENT:
            return ReverseResult("pending")
        override = self._conn.execute("SELECT * FROM manual_override WHERE direction = 'reverse' "
                                      "AND ma_provider_uri = ?", (key,)).fetchone()
        if override is not None:
            if override["blocked"]:
                return ReverseResult("unmatched", reason="blocked")
            return ReverseResult("matched", override["spotify_id"])
        row = self._mapping(key)
        if row is not None and row["status"] == "matched":
            return ReverseResult("matched", row["spotify_id"])
        if (row is not None and row["status"] == "unmatched" and row["unmatched_retry_at"]
                and parse_iso(row["unmatched_retry_at"]) > self._now()):
            return ReverseResult("unmatched", reason=row["unmatched_reason"])
        library_uri = ma_uri if ma_uri.startswith("library://") else None
        try:
            item = item if item is not None else await self._ma.get_item(key)
            isrcs = [str(v).upper() for k, v in (item.get("external_ids") or []) if k == "isrc"]
            if not isrcs:
                return self._save(key, library_uri, None, "unmatched", reason="no_isrc")
            cands = await self._spotify.search_isrc(isrcs[0])
        except TRANSIENT:
            return ReverseResult("pending")
        choice = choose_spotify(int(item.get("duration") or 0), (item.get("metadata") or {}).get("explicit"),
                                (item.get("album") or {}).get("name", ""), cands)
        if choice is None:
            return self._save(key, library_uri, isrcs[0], "unmatched",
                              reason="duration_mismatch" if cands else "no_match")
        return self._save(key, library_uri, isrcs[0], "matched", spotify_id=choice.id)
