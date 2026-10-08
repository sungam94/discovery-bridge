"""Collects artist facts and hands Music Assistant a text per artist (via artist_info.json, read by the
spotify_bridge provider), then asks MA to refresh the library artists whose text changed."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from bridge.artist_info.compose import compose
from bridge.artist_info.sources import SourceError
from bridge.genre.musicbrainz import MusicBrainzError
from bridge.genre.store import artist_key, save_artist

log = logging.getLogger("bridge.artist_info")
MAX_AGE = timedelta(days=30)
HANDOVER_TRIES = 3  # MA sometimes skips the bio (no MusicBrainz id found that time); give up after this many
FILE_VERSION = 1
_LIVE = "p.ma_playlist_id IS NOT NULL AND p.stale = 0 AND p.published_snapshot_id IS NOT NULL"
_HEARD = ("completed", "listened")


def write_artist_file(directory: Path, texts: dict[str, str]) -> None:
    """Replace artist_info.json atomically, so MA never reads a half-written file."""
    directory = Path(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".artist-info-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": FILE_VERSION, "artists": texts}, f, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / "artist_info.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ArtistInfoWorker:
    def __init__(self, conn: sqlite3.Connection, ma, mb, lastfm, wiki, now: Callable[[], datetime],
                 write_file: Callable[[dict[str, str]], None]) -> None:
        self._conn, self._ma, self._mb, self._lastfm, self._wiki = conn, ma, mb, lastfm, wiki
        self._now, self._write_file = now, write_file
        self._tries: dict[str, int] = {}

    async def run_once(self, batch: int) -> int:
        library = await self._ma.library_artists()  # [(name, item_id)]
        fetched = 0
        for name in self._pending([n for n, _ in library], batch):
            try:
                parts = await self._fetch(name)
            except (SourceError, MusicBrainzError) as exc:
                log.warning("artist facts for one artist failed (%s); retried later", exc)
                continue
            raw = {"lastfm": (parts["lastfm"] or {}).pop("raw", None), "musicbrainz": (parts["mb"] or {}).pop("raw", None)}
            self._conn.execute(
                "INSERT INTO artist_info(artist_key, name, parts, raw, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(artist_key) DO UPDATE SET name = excluded.name, parts = excluded.parts, "
                "raw = excluded.raw, fetched_at = excluded.fetched_at",
                (artist_key(name), name, json.dumps(parts), json.dumps(raw), self._now().isoformat()))
            fetched += 1
        await self._publish(dict((artist_key(n), i) for n, i in library))
        return fetched

    def _pending(self, library_names: list[str], batch: int) -> list[str]:
        fresh = {r["artist_key"] for r in self._conn.execute(
            "SELECT artist_key FROM artist_info WHERE fetched_at > ? AND raw IS NOT NULL",  # no raw: fetch again
            ((self._now() - MAX_AGE).isoformat(),))}
        playlist_names = [a.strip() for r in self._conn.execute(
            "SELECT t.artist FROM playlist p JOIN playlist_snapshot_item i ON i.snapshot_id = p.published_snapshot_id "
            f"JOIN source_track t ON t.id = i.source_track_id WHERE {_LIVE} ORDER BY p.slot, i.position")
            for a in r["artist"].split("\x1f") if a.strip()]
        out, seen = [], set()
        for name in library_names + playlist_names:   # library artists first: only they have pages in MA
            key = artist_key(name)
            if key in fresh or key in seen:
                continue
            seen.add(key)
            out.append(name)
            if len(out) >= batch:
                break
        return out

    async def _fetch(self, name: str) -> dict:
        lastfm = await self._lastfm.artist(name)
        row = self._conn.execute("SELECT mbid FROM artist_genre WHERE artist_key = ?", (artist_key(name),)).fetchone()
        mbid = row["mbid"] if row else None
        if row is None:
            mbid, genres = await self._mb.artist(name)
            save_artist(self._conn, name, mbid, genres, self._now())   # the genre worker needs it too
        mb = await self._mb.details(mbid) if mbid else None
        bio, source = (lastfm or {}).get("bio") or "", "Last.fm"
        if not bio and mb and mb.get("wikidata"):
            bio, source = await self._wiki.intro(mb["wikidata"]) or "", "Wikipedia"
        return {"bio": bio, "bio_source": source if bio else None, "mb": mb, "lastfm": lastfm}

    def _local_facts(self) -> Callable[[str], dict]:
        """Playlists and play counts per artist, built once per pass (one scan instead of one per artist)."""
        isrcs: dict[str, set] = {}
        ids: dict[str, set] = {}
        for r in self._conn.execute("SELECT isrc, provider_item_id, artist FROM source_track WHERE provider = 'spotify'"):
            for a in r["artist"].split("\x1f"):
                key = artist_key(a)
                if r["isrc"]:
                    isrcs.setdefault(key, set()).add(r["isrc"])
                ids.setdefault(key, set()).add(r["provider_item_id"])
        heard_isrc, heard_id = {}, {}
        for r in self._conn.execute(
                f"SELECT isrc, spotify_id FROM taste_event WHERE outcome IN ({','.join('?' * len(_HEARD))})", _HEARD):
            if r["isrc"]:
                heard_isrc[r["isrc"]] = heard_isrc.get(r["isrc"], 0) + 1
            elif r["spotify_id"]:
                heard_id[r["spotify_id"]] = heard_id.get(r["spotify_id"], 0) + 1
        playlists: dict[str, list[str]] = {}
        for r in self._conn.execute(
                "SELECT DISTINCT p.name, t.artist FROM playlist p JOIN playlist_snapshot_item i "
                "ON i.snapshot_id = p.published_snapshot_id JOIN source_track t ON t.id = i.source_track_id "
                f"WHERE {_LIVE} ORDER BY p.name"):
            for a in r["artist"].split("\x1f"):
                names = playlists.setdefault(artist_key(a), [])
                name = r["name"].removeprefix("Spotify · ")
                if name not in names:
                    names.append(name)

        def facts(key: str) -> dict:
            plays = sum(heard_isrc.get(i, 0) for i in isrcs.get(key, ())) + \
                sum(heard_id.get(i, 0) for i in ids.get(key, ()))
            return {"playlists": playlists.get(key, []), "plays": plays}
        return facts

    async def _publish(self, library: dict[str, str]) -> None:
        local = self._local_facts()
        texts, changed = {}, {}
        for r in self._conn.execute("SELECT artist_key, parts, text FROM artist_info ORDER BY artist_key").fetchall():
            parts = json.loads(r["parts"])
            text = compose(parts["bio"], parts["bio_source"], parts["mb"], parts["lastfm"], local(r["artist_key"]))
            if text:
                texts[r["artist_key"]] = text
            if text != r["text"]:
                changed[r["artist_key"]] = text
        if not changed:
            return
        self._write_file(texts)
        for key, text in changed.items():
            if key in library:
                try:
                    shown = await self._ma.refresh_artist(library[key])
                except Exception as exc:  # the text is not marked as handed over, so the next pass tries again
                    log.warning("MA refresh of a library artist failed (%s)", type(exc).__name__)
                    continue
                if text and shown.strip() != text.strip():   # MA kept another bio (e.g. SoundCloud's)
                    self._tries[key] = self._tries.get(key, 0) + 1
                    if self._tries[key] < HANDOVER_TRIES:
                        continue
                    log.info("MA keeps another bio for a library artist after %d refreshes; leaving it",
                             HANDOVER_TRIES)
                self._tries.pop(key, None)
            self._conn.execute("UPDATE artist_info SET text = ? WHERE artist_key = ?", (text, key))
