"""The "sources" part of layout.json: what the MA plugin draws on the covers of the TIDAL and SoundCloud
playlists of the Discover rows. MA is not asked to do anything for them; the plugin redraws them itself when
their entry changes.

A TIDAL playlist is listed once all its artists are looked up, so its cover is not redrawn while that fills.
Every genre listed has a hue (one without a place on the wheel is skipped), so the plugin can colour them all."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from bridge.genre.store import fresh_keys, source_complete, source_genre_shares
from bridge.genre.wheel import hues_for

MAX_GENRES = 6
TILES = 6   # album covers on a TIDAL cover


def _all_tiles(conn: sqlite3.Connection) -> dict[str, list[list[str]]]:
    """{playlist uri: the first TILES different album covers of its tracks, in track order, as [path, provider]}."""
    out: dict[str, list[list[str]]] = {}
    for r in conn.execute("SELECT playlist_uri, image FROM source_playlist_track WHERE image IS NOT NULL "
                          "ORDER BY playlist_uri, position"):
        tiles = out.setdefault(r["playlist_uri"], [])
        try:
            path, provider = json.loads(r["image"])
        except (TypeError, ValueError):
            continue
        if len(tiles) < TILES and [path, provider] not in tiles:
            tiles.append([path, provider])
    return out


def source_entries(conn: sqlite3.Connection, now: datetime, hue_overrides: dict | None,
                   moods: dict) -> tuple[dict[str, dict], dict[str, float]]:
    """({playlist uri: {"name", "source", "genres", "moods"?, "tiles"?}}, {genre: hue} of the genres used).
    "tiles" (TIDAL only) are album covers; being part of the entry, a new set renames the cover in the plugin."""
    entries: dict[str, dict] = {}
    hues: dict[str, float] = {}
    # read once for all playlists: this runs on every genre worker pass
    fresh, artist_genres, placed, all_tiles = fresh_keys(conn, now), {}, {}, None
    for r in conn.execute("SELECT uri, name, source FROM source_playlist ORDER BY uri").fetchall():
        if not source_complete(conn, r["uri"], now, fresh):
            continue
        genres = []
        for genre, tracks in source_genre_shares(conn, r["uri"], limit=None, cache=artist_genres):
            if genre not in placed:
                placed[genre] = hues_for(conn, [genre], hue_overrides or {}, now)[genre]
            hue = placed[genre]
            if hue is None:
                continue
            hues[genre] = hue
            genres.append([genre, tracks])
            if len(genres) >= MAX_GENRES:
                break
        entry = {"name": r["name"], "source": r["source"], "genres": genres}
        if r["uri"] in moods:
            entry["moods"] = moods[r["uri"]]
        if r["source"] == "tidal":
            all_tiles = _all_tiles(conn) if all_tiles is None else all_tiles
            if tiles := all_tiles.get(r["uri"]):
                entry["tiles"] = tiles
        entries[r["uri"]] = entry
    return entries, hues
