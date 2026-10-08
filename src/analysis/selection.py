"""Which tracks to analyse next: those of the live published playlists without a result yet, then those of the
TIDAL playlists of MA's Discover rows (collected by the bridge, src/bridge/sources).

The playlist closest to complete goes first, so the covers get their mood footer one after the other."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from analysis.store import iso

RETRY_ERRORS_AFTER = timedelta(days=7)
LIVE_TRACKS = (
    "SELECT p.name, t.isrc FROM playlist p "
    "JOIN playlist_snapshot_item i ON i.snapshot_id = p.published_snapshot_id "
    "JOIN source_track t ON t.id = i.source_track_id "
    "WHERE p.ma_playlist_id IS NOT NULL AND p.stale = 0 AND p.published_snapshot_id IS NOT NULL "
    "AND t.isrc IS NOT NULL AND t.isrc != '' ORDER BY p.name, i.position")
SOURCE_TRACKS = (
    "SELECT p.uri, t.isrc FROM source_playlist p JOIN source_playlist_track t ON t.playlist_uri = p.uri "
    "WHERE p.source = 'tidal' AND t.isrc IS NOT NULL AND t.isrc != '' ORDER BY p.uri, t.position")


def settled(sound: sqlite3.Connection, now: datetime) -> set[str]:
    """ISRCs with a result that stands; an error is tried again after RETRY_ERRORS_AFTER."""
    return {r[0] for r in sound.execute("SELECT isrc FROM track_sound WHERE status != 'error' OR analysed_at > ?",
                                        (iso(now - RETRY_ERRORS_AFTER),)).fetchall()}


def _has_sources(bridge: sqlite3.Connection) -> bool:
    """The bridge adds the tables in a migration; the container may read an older database."""
    return bridge.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                          "AND name IN ('source_playlist', 'source_playlist_track')").fetchone()[0] == 2


def _closest_first(rows: list[tuple[str, str]], done: set[str], seen: set[str]) -> list[str]:
    by_playlist: dict[str, list[str]] = {}
    for key, isrc in rows:
        if isrc not in done:
            by_playlist.setdefault(key, []).append(isrc)
    out: list[str] = []
    for key in sorted(by_playlist, key=lambda k: (len(set(by_playlist[k])), k)):
        for isrc in by_playlist[key]:
            if isrc not in seen:
                seen.add(isrc)
                out.append(isrc)
    return out


def pending(bridge: sqlite3.Connection, sound: sqlite3.Connection, now: datetime,
            limit: int | None = None) -> list[str]:
    done, seen = settled(sound, now), set()
    out = _closest_first(bridge.execute(LIVE_TRACKS).fetchall(), done, seen)
    if _has_sources(bridge):
        out += _closest_first(bridge.execute(SOURCE_TRACKS).fetchall(), done, seen)
    return out[:limit] if limit is not None else out
