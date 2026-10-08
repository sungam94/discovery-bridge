"""Spec §5.1 source context: which bridge playlist a play came from, and whether MA's autoplay chose it."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from websockets.exceptions import WebSocketException

from bridge.ma.client import MaError, tidal_uri
from bridge.resolve.reverse import canonical_uri

TRANSIENT = (MaError, OSError, TimeoutError, WebSocketException, KeyError)
MEMBERS_TTL = timedelta(minutes=10)
_LISTING = {"playlist": "music/playlists/playlist_tracks", "album": "music/albums/album_tracks"}


def bridge_source(conn: sqlite3.Connection, ma_playlist_id: str) -> tuple[str, int] | None:
    row = conn.execute("SELECT spotify_id, published_snapshot_id FROM playlist WHERE ma_playlist_id = ? "
                       "AND published_snapshot_id IS NOT NULL", (str(ma_playlist_id),)).fetchone()
    if row is not None:
        return row["spotify_id"], row["published_snapshot_id"]
    row = conn.execute("SELECT spotify_id, id FROM playlist_snapshot WHERE ma_history_playlist_id = ?",
                       (str(ma_playlist_id),)).fetchone()
    return (row["spotify_id"], row["id"]) if row is not None else None


def snapshot_has(conn: sqlite3.Connection, snapshot_id: int, uri: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM playlist_snapshot_item i JOIN source_track s ON s.id = i.source_track_id "
        "JOIN track_mapping m ON m.direction = 'forward' AND m.status = 'matched' AND m.spotify_id = s.provider_item_id "
        "WHERE i.snapshot_id = ? AND m.ma_provider_uri = ? LIMIT 1", (snapshot_id, uri)).fetchone() is not None


class SourceResolver:
    def __init__(self, conn: sqlite3.Connection, ma, now: Callable[[], datetime]) -> None:
        self._conn, self._ma, self._now = conn, ma, now
        self._members: dict[str, tuple[datetime, frozenset[str]]] = {}

    async def _source_members(self, uri: str, media_type: str) -> frozenset[str]:
        cached = self._members.get(uri)
        if cached is not None and self._now() - cached[0] < MEMBERS_TTL:
            return cached[1]
        provider, item_id = uri.split("://", 1)[0], uri.rsplit("/", 1)[1]
        tracks = await self._ma.call(_LISTING[media_type], item_id=item_id, provider_instance_id_or_domain=provider)
        members = frozenset({t["uri"] for t in tracks} | {tidal_uri(t) for t in tracks})
        self._members[uri] = (self._now(), members)
        return members

    async def context(self, queue_id: str, item_uri: str) -> tuple[str | None, bool]:
        try:
            q = await self._ma.call("player_queues/get", queue_id=queue_id)
            sources = q.get("sources") or []
            if not sources:
                return None, False
            autoplay = bool(q.get("autoplay_enabled") or q.get("dont_stop_the_music_enabled"))
            src_uri, media_type = sources[0].get("uri", ""), sources[0].get("media_type")
            if src_uri.startswith("radio_playlist://"):  # MA 2.10.4: "radio" is a dynamic playlist MA fills
                return None, True
            item_uri = (await canonical_uri(self._ma, item_uri))[0]  # library tracks arrive as library://
            if media_type not in _LISTING:
                return None, autoplay and src_uri != item_uri
            if src_uri.startswith("library://playlist/"):
                pid = src_uri.rsplit("/", 1)[1]
                bridge = bridge_source(self._conn, pid)
                if bridge is not None:
                    if snapshot_has(self._conn, bridge[1], item_uri):
                        return pid, False
                    return None, autoplay
            member = item_uri in await self._source_members(src_uri, media_type)
            return None, (not member) and autoplay
        except TRANSIENT:
            return None, False
