"""Adds the artists of tracks heard in Music Assistant to its library (only artists actually
played, not every artist of every playlist). MA's library sync-back also follows them on the streaming service."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from bridge.genre.store import artist_key
from bridge.ma.client import MaError

log = logging.getLogger("bridge.played_artists")
HEARD = ("completed", "listened")


class PlayedArtists:
    def __init__(self, conn: sqlite3.Connection, ma, now: Callable[[], datetime]) -> None:
        self._conn, self._ma, self._now = conn, ma, now

    async def run_once(self, limit: int) -> int:
        tracks = [r["ma_item_uri"] for r in self._conn.execute(
            f"SELECT DISTINCT e.ma_item_uri FROM taste_event e WHERE e.outcome IN ({','.join('?' * len(HEARD))}) "
            "AND e.ma_item_uri NOT IN (SELECT ma_item_uri FROM played_track) ORDER BY e.started_at", HEARD)]
        if not tracks:
            return 0
        in_library = {artist_key(n) for n, _ in await self._ma.library_artists()}
        added = 0
        for uri in tracks:
            try:
                item = await self._ma.get_item(uri)
            except MaError as exc:
                log.warning("played track lookup failed (%s); retried later", exc)
                continue
            pending = False
            for a in (item or {}).get("artists") or []:
                a_uri, name = a.get("uri") or "", a.get("name") or ""
                if not a_uri or a_uri.startswith("library://") or artist_key(name) in in_library or \
                        self._conn.execute("SELECT 1 FROM played_artist WHERE artist_uri = ?", (a_uri,)).fetchone():
                    continue
                if added >= limit:
                    pending = True
                    break
                try:
                    await self._ma.add_to_library(a_uri)
                except MaError as exc:
                    log.warning("adding a played artist to the MA library failed (%s); retried later", exc)
                    pending = True
                    continue
                self._conn.execute("INSERT INTO played_artist(artist_uri, name, added_at) VALUES (?, ?, ?)",
                                   (a_uri, name, self._now().isoformat()))
                in_library.add(artist_key(name))
                added += 1
                log.info("played artist added to the MA library: %s", name)
            if pending:
                return added
            self._conn.execute("INSERT OR IGNORE INTO played_track(ma_item_uri, checked_at) VALUES (?, ?)",
                               (uri, self._now().isoformat()))
        return added
