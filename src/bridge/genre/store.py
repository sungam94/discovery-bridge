"""Artist genres (from MusicBrainz) and the genres of a published playlist derived from them."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from bridge.genre.wheel import anchor_hue

MAX_AGE = timedelta(days=30)
ARTIST_GENRES = 3
# too broad to say anything on a cover; used only when an artist has nothing more specific
UMBRELLA = frozenset({"electronic", "rock", "pop", "metal", "experimental", "alternative", "alternative rock",
                      "indie", "indie rock", "instrumental", "psychedelic"})
_LIVE = "p.ma_playlist_id IS NOT NULL AND p.stale = 0 AND p.published_snapshot_id IS NOT NULL"


def artist_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _artists(field: str) -> list[str]:
    return [a.strip() for a in field.split("\x1f") if a.strip()]


def save_artist(conn: sqlite3.Connection, name: str, mbid: str | None, genres: list[tuple[str, int]],
                now: datetime) -> None:
    conn.execute(
        "INSERT INTO artist_genre(artist_key, name, mbid, genres, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(artist_key) DO UPDATE SET name = excluded.name, mbid = excluded.mbid, genres = excluded.genres, "
        "fetched_at = excluded.fetched_at",
        (artist_key(name), name, mbid, json.dumps(genres), now.isoformat()))


def fresh_keys(conn: sqlite3.Connection, now: datetime) -> set[str]:
    return {r["artist_key"] for r in conn.execute("SELECT artist_key FROM artist_genre WHERE fetched_at > ?",
                                                  ((now - MAX_AGE).isoformat(),))}


def _track_artists(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[list[str]]:
    return [_artists(r["artist"]) for r in conn.execute(
        "SELECT t.artist FROM playlist p JOIN playlist_snapshot_item i ON i.snapshot_id = p.published_snapshot_id "
        f"JOIN source_track t ON t.id = i.source_track_id WHERE {_LIVE} AND {where} ORDER BY p.slot, i.position", params)]


def _source_artists(conn: sqlite3.Connection, uri: str | None = None) -> list[list[str]]:
    """Artists per track of the TIDAL playlists from the Discover rows (SoundCloud has its own genre tags)."""
    where, params = ("AND p.uri = ?", (uri,)) if uri is not None else ("", ())
    return [_artists(r["artists"]) for r in conn.execute(
        "SELECT t.artists FROM source_playlist p JOIN source_playlist_track t ON t.playlist_uri = p.uri "
        f"WHERE p.source = 'tidal' {where} ORDER BY p.uri, t.position", params)]


def pending_artists(conn: sqlite3.Connection, now: datetime, limit: int) -> list[str]:
    """Artists of live published playlists, then of the TIDAL Discover playlists, that were never looked up, or
    whose row is older than MAX_AGE."""
    fresh, seen, out = fresh_keys(conn, now), set(), []
    for artists in _track_artists(conn, "1 = 1") + _source_artists(conn):
        for a in artists:
            key = artist_key(a)
            if key in fresh or key in seen:
                continue
            seen.add(key)
            out.append(a)
            if len(out) >= limit:
                return out
    return out


def playlist_complete(conn: sqlite3.Connection, name: str, now: datetime) -> bool:
    fresh = fresh_keys(conn, now)
    return all(artist_key(a) in fresh for artists in _track_artists(conn, "p.name = ?", (name,)) for a in artists)


def _artist_genres(conn: sqlite3.Connection, key: str, cache: dict[str, list[str]]) -> list[str]:
    if key not in cache:
        row = conn.execute("SELECT genres FROM artist_genre WHERE artist_key = ?", (key,)).fetchone()
        ranked = [g for g, _ in sorted(json.loads(row["genres"]), key=lambda x: -x[1])] if row else []
        specific = [g for g in ranked if g not in UMBRELLA]
        cache[key] = specific[:ARTIST_GENRES] or ranked[:1]
    return cache[key]


def _ranked(scores: Counter[str], limit: int | None) -> list[list]:
    return [[g, n] for g, n in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _artist_shares(conn: sqlite3.Connection, tracks: list[list[str]], limit: int | None,
                   cache: dict[str, list[str]] | None = None) -> list[list]:
    scores: Counter[str] = Counter()
    cache = {} if cache is None else cache
    for artists in tracks:
        scores.update({g for a in artists for g in _artist_genres(conn, artist_key(a), cache)})
    return _ranked(scores, limit)


def playlist_genre_shares(conn: sqlite3.Connection, name: str, limit: int = 6) -> list[list]:
    """The playlist's leading genres as [genre, tracks], most tracks first. A track counts for a genre when one
    of its artists has it."""
    return _artist_shares(conn, _track_artists(conn, "p.name = ?", (name,)), limit)


def _source_kind(conn: sqlite3.Connection, uri: str) -> str | None:
    row = conn.execute("SELECT source FROM source_playlist WHERE uri = ?", (uri,)).fetchone()
    return row["source"] if row else None


def _tag(tag: str) -> str:
    return " ".join(str(tag).casefold().split())


def source_genre_shares(conn: sqlite3.Connection, uri: str, limit: int | None = 6,
                        cache: dict[str, list[str]] | None = None) -> list[list]:
    """Genres of a TIDAL or SoundCloud Discover playlist as [genre, tracks], most tracks first. TIDAL: from the
    artists, as for the own playlists. SoundCloud: the uploaders' genre tags that the colour wheel places by name
    (a free text tag like "Sectio Aurea RMX" is no genre). cache keeps the artists' genres across calls."""
    if _source_kind(conn, uri) != "soundcloud":
        return _artist_shares(conn, _source_artists(conn, uri), limit, cache)
    scores: Counter[str] = Counter()
    for r in conn.execute("SELECT genres FROM source_playlist_track WHERE playlist_uri = ?", (uri,)):
        scores.update({t for t in map(_tag, json.loads(r["genres"])) if t and anchor_hue(t) is not None})
    specific = Counter({g: n for g, n in scores.items() if g not in UMBRELLA})
    return _ranked(specific or scores, limit)   # a track has one tag: broad ones only when nothing else


def source_complete(conn: sqlite3.Connection, uri: str, now: datetime, fresh: set[str] | None = None) -> bool:
    """Its tracks are read and, for TIDAL, every artist is looked up (SoundCloud needs no lookups). fresh is
    fresh_keys(conn, now), when the caller checks several playlists."""
    if conn.execute("SELECT 1 FROM source_playlist_track WHERE playlist_uri = ? LIMIT 1", (uri,)).fetchone() is None:
        return False
    if _source_kind(conn, uri) != "tidal":
        return True
    fresh = fresh_keys(conn, now) if fresh is None else fresh
    return all(artist_key(a) in fresh for artists in _source_artists(conn, uri) for a in artists)


def all_playlist_genres(conn: sqlite3.Connection) -> dict[str, list[list]]:
    names = [r["name"] for r in conn.execute(f"SELECT p.name FROM playlist p WHERE {_LIVE} ORDER BY p.name")]
    return {n: g for n in names if (g := playlist_genre_shares(conn, n))}
