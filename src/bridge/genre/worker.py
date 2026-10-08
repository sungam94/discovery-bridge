"""Looks up genres for the artists of the published playlists and gets the playlist covers redrawn.

A cover is refreshed only when every artist of its playlist has been looked up and the playlist's genres or its
mood footer (from the sound analysis) differ from the ones its cover was last drawn with, so a slow first fill
does not redraw a cover again and again. The check runs on every pass, also when no artist is pending.

The TIDAL and SoundCloud playlists of the Discover rows are not MA library items: for them the worker only
rewrites the layout file when their entries change, and the MA plugin redraws their covers itself."""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime

from bridge.genre.musicbrainz import MusicBrainzError
from bridge.genre.store import pending_artists, playlist_complete, playlist_genre_shares, save_artist
from bridge.genre.wheel import hues_for
from bridge.repo import get_setting, set_setting
from bridge.sources.covers import source_entries

log = logging.getLogger("bridge.genre")
SETTING = "cover_genres"  # {playlist name: {"genres", "style", "hues", "moods", "version"} its last cover}
SOURCES_SETTING = "source_covers"  # the "sources" entries and their hues last written to the layout file
COVER_VERSION = 2  # raised when the plugin draws covers differently (2: brand frame), so each is redrawn once


class GenreWorker:
    def __init__(self, conn: sqlite3.Connection, mb, now: Callable[[], datetime], write_layout: Callable[[], None],
                 refresh_cover: Callable[[str], Awaitable[None]], style: dict,
                 hue_overrides: dict | None = None, moods: Callable[[], dict | None] | None = None,
                 source_moods: Callable[[], dict | None] | None = None) -> None:
        self._conn, self._mb, self._now = conn, mb, now
        self._write_layout, self._refresh_cover = write_layout, refresh_cover
        self._style = json.dumps(style, sort_keys=True)  # a changed cover style redraws every cover once
        self._hue_overrides = hue_overrides or {}
        self._moods = moods or dict  # {playlist name: mood entry}, as written to the layout file; None if unreadable
        self._source_moods = source_moods or dict  # the same for the Discover playlists, by uri

    async def run_once(self, batch: int) -> int:
        fetched = 0
        for name in pending_artists(self._conn, self._now(), batch):
            try:
                mbid, genres = await self._mb.artist(name)
            except MusicBrainzError as exc:
                log.warning("genre lookup failed for an artist (%s); retried later", exc)
                continue
            save_artist(self._conn, name, mbid, genres, self._now())
            fetched += 1
        self._sources_changed()
        await self._refresh_changed()
        return fetched

    def _sources_changed(self) -> None:
        moods = self._source_moods()
        if moods is None:  # as for the own covers: an unreadable sound.sqlite must not take the footers off
            return
        entries, hues = source_entries(self._conn, self._now(), self._hue_overrides, moods)
        target = {"sources": entries, "hues": hues}
        if target == json.loads(get_setting(self._conn, SOURCES_SETTING) or '{"sources": {}, "hues": {}}'):
            return
        self._write_layout()
        set_setting(self._conn, SOURCES_SETTING, json.dumps(target, sort_keys=True))

    async def _refresh_changed(self) -> None:
        moods = self._moods()
        if moods is None:  # sound.sqlite cannot be read just now; redrawing would take the footers off
            log.warning("cover check skipped: the playlist moods cannot be read")
            return
        drawn: dict[str, object] = json.loads(get_setting(self._conn, SETTING) or "{}")
        changed = []
        for row in self._conn.execute("SELECT name, ma_playlist_id FROM playlist WHERE ma_playlist_id IS NOT NULL "
                                      "AND stale = 0 AND published_snapshot_id IS NOT NULL ORDER BY name").fetchall():
            shares = playlist_genre_shares(self._conn, row["name"])
            hues = hues_for(self._conn, [g for g, _ in shares], self._hue_overrides, self._now())
            target = {"genres": shares, "style": self._style, "hues": [hues[g] for g, _ in shares],
                      "moods": moods.get(row["name"]), "version": COVER_VERSION}
            if target != drawn.get(row["name"]) and playlist_complete(self._conn, row["name"], self._now()):
                changed.append((row["name"], row["ma_playlist_id"], target))
        if not changed:
            return
        self._write_layout()  # the MA plugin reads genres and moods from the layout file when it draws the cover
        for name, pid, target in changed:
            try:
                await self._refresh_cover(pid)
            except Exception as exc:
                log.warning("cover refresh failed for MA playlist %s (%s); retried later", pid, type(exc).__name__)
                continue
            drawn[name] = target
            set_setting(self._conn, SETTING, json.dumps(drawn))
