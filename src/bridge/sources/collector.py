"""Reads the TIDAL and SoundCloud playlists of MA's Discover rows (config source_rows) and their tracks, so their
covers can show genres and moods like the bridge's own playlists.

All calls go one after the other with a pause between them. TIDAL lists a playlist's tracks without ISRCs; the
full track has one, so each TIDAL track is asked once (also when it has none). MA answers a provider error with an
empty row, so an empty or missing row keeps its playlists until they are KEEP_UNSEEN old."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from bridge.ma.client import MaError

log = logging.getLogger("bridge.sources")
KEEP_UNSEEN = timedelta(days=7)
SEP = "\x1f"
ROW_NAMES_LOGGED = 8


def source_of(provider: str) -> str:
    """'tidal--test0001' -> 'tidal'."""
    return provider.split("--", 1)[0]


def row_matches(source: str, row_name: str, rows: Mapping[str, tuple[str, ...]]) -> bool:
    name = row_name.casefold()
    return any(name.startswith(p.casefold()) for p in rows.get(source, ()))


def _thumb(track: dict) -> str | None:
    """JSON [path, provider] of the track's own thumb, else its album's; None when MA lists neither."""
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    candidates = [*((track.get("metadata") or {}).get("images") or []), track.get("image"), album.get("image")]
    for img in candidates:
        if isinstance(img, dict) and img.get("type", "thumb") == "thumb" and img.get("path") and img.get("provider"):
            return json.dumps([str(img["path"]), str(img["provider"])])
    return None


def _isrc(item: dict | None) -> str | None:
    for pair in (item or {}).get("external_ids") or []:
        if len(pair) == 2 and pair[0] == "isrc" and pair[1]:
            return str(pair[1]).strip().upper()
    return None


class SourceCollector:
    def __init__(self, conn: sqlite3.Connection, ma, now: Callable[[], datetime],
                 rows: Mapping[str, tuple[str, ...]], pause_s: float = 0.5) -> None:
        self._conn, self._ma, self._now, self._rows, self._pause = conn, ma, now, rows, pause_s

    async def _call(self, method, *args):
        result = await method(*args)
        if self._pause:
            await asyncio.sleep(self._pause)
        return result

    async def run_once(self) -> dict[str, int]:
        now = self._now()
        seen: set[str] = set()
        answered: set[tuple[str, str]] = set()  # (source, row) that returned items
        isrcs = 0
        names: dict[str, list[str]] = {}   # row names per source, for the warning below
        matched: set[str] = set()
        rows = await self._call(self._ma.recommendation_rows)
        if not rows:   # MA lists rows from every provider; none at all means it is still starting
            raise MaError("MA lists no Discover rows yet")
        for row in rows:
            provider, name = str(row.get("provider") or ""), str(row.get("name") or "")
            source = source_of(provider)
            names.setdefault(source, []).append(name)
            if not row_matches(source, name, self._rows):
                continue
            matched.add(source)
            try:
                listed = await self._call(self._ma.recommendation_items, provider, str(row["item_id"]))
            except MaError as exc:
                log.warning("Discover row of %s cannot be read (%s); its playlists are kept", source, exc)
                continue
            items = [i for i in listed if i.get("media_type") == "playlist" and i.get("uri")]
            if items:
                answered.add((source, name))
            for item in items:
                seen.add(item["uri"])
                isrcs += await self._store_playlist(item, source, name, now)
        self._drop(seen, answered, now)
        for source, prefixes in self._rows.items():
            if prefixes and source not in matched:
                log.warning("no Discover row of %s starts with %s; its rows: %s", source, list(prefixes),
                            names.get(source, [])[:ROW_NAMES_LOGGED])
        out = {"playlists": len(seen), "isrcs_fetched": isrcs}
        log.info("source playlists: %s", out)
        return out

    async def _store_playlist(self, item: dict, source: str, row: str, now: datetime) -> int:
        uri = item["uri"]
        self._conn.execute(
            "INSERT INTO source_playlist(uri, name, source, row_name, fetched_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(uri) DO UPDATE SET name = excluded.name, source = excluded.source, "
            "row_name = excluded.row_name, fetched_at = excluded.fetched_at",
            (uri, item.get("name") or "", source, row, now.isoformat()))
        try:
            listed = await self._call(self._ma.provider_playlist_tracks, str(item.get("item_id")),
                                      str(item.get("provider")))
        except MaError as exc:
            log.warning("tracks of a %s playlist cannot be read (%s); kept as they were", source, exc)
            return 0
        if not listed:
            return 0
        listed = sorted(listed, key=lambda t: t.get("position") or 0)
        fetched, rows = 0, []
        for pos, t in enumerate(listed):
            isrc = _isrc(t)
            if isrc is None and source == "tidal" and t.get("uri"):
                isrc, asked = await self._tidal_isrc(t["uri"])
                fetched += asked
            artists = SEP.join(a["name"].strip() for a in t.get("artists") or [] if (a.get("name") or "").strip())
            genres = [str(g) for g in ((t.get("metadata") or {}).get("genres") or [])]
            rows.append((uri, pos, t.get("uri") or "", artists, isrc, json.dumps(genres, ensure_ascii=False),
                         _thumb(t)))
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM source_playlist_track WHERE playlist_uri = ?", (uri,))
            self._conn.executemany("INSERT INTO source_playlist_track(playlist_uri, position, track_uri, artists, "
                                   "isrc, genres, image) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return fetched

    async def _tidal_isrc(self, track_uri: str) -> tuple[str | None, int]:
        """(ISRC, 1 if MA was asked); a known track is never asked again, a failed ask is tried next pass."""
        row = self._conn.execute("SELECT isrc FROM source_track_isrc WHERE track_uri = ?", (track_uri,)).fetchone()
        if row is not None:
            return row["isrc"], 0
        try:
            isrc = _isrc(await self._call(self._ma.get_item, track_uri))
        except MaError as exc:
            log.warning("TIDAL track lookup failed (%s); tried again next pass", exc)
            return None, 1
        self._conn.execute("INSERT INTO source_track_isrc(track_uri, isrc, fetched_at) VALUES (?, ?, ?)",
                           (track_uri, isrc, self._now().isoformat()))
        return isrc, 1

    def _drop(self, seen: set[str], answered: set[tuple[str, str]], now: datetime) -> None:
        old = (now - KEEP_UNSEEN).isoformat()
        for r in self._conn.execute("SELECT uri, source, row_name, fetched_at FROM source_playlist").fetchall():
            if r["uri"] in seen:
                continue
            if (not row_matches(r["source"], r["row_name"], self._rows)
                    or (r["source"], r["row_name"]) in answered or r["fetched_at"] < old):
                self._conn.execute("DELETE FROM source_playlist WHERE uri = ?", (r["uri"],))
