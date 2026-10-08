"""Mood footer of the playlist covers, from the sound analysis results in sound.sqlite (src/analysis).

The bridge only reads that file. A playlist gets a mood entry once nearly all its tracks are analysed, so a
footer does not change with every new result; the MA plugin turns the entry into the footer text."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

MIN_COVERAGE = 0.9   # share of a playlist's ISRCs that need a result (any status)
MIN_DONE = 5         # tracks actually analysed
SECOND_WORD_MIN = 0.6  # a second word is shown when its score reaches this share of the first
# MTG-Jamendo themes that describe a use or a setting rather than a mood
NOT_MOODS = frozenset({
    "background", "film", "movie", "trailer", "advertising", "commercial", "corporate", "documentary", "children",
    "christmas", "holiday", "game", "sport", "summer", "travel", "retro", "motivational", "action", "adventure",
    "drama", "funny", "nature", "melodic"})
LIVE_TRACKS = (
    "SELECT p.name, t.isrc FROM playlist p "
    "JOIN playlist_snapshot_item i ON i.snapshot_id = p.published_snapshot_id "
    "JOIN source_track t ON t.id = i.source_track_id "
    "WHERE p.ma_playlist_id IS NOT NULL AND p.stale = 0 AND p.published_snapshot_id IS NOT NULL "
    "AND t.isrc IS NOT NULL AND t.isrc != ''")
# TIDAL playlists of the Discover rows (src/bridge/sources); SoundCloud tracks have no ISRC
SOURCE_TRACKS = (
    "SELECT p.uri, t.isrc FROM source_playlist p JOIN source_playlist_track t ON t.playlist_uri = p.uri "
    "WHERE p.source = 'tidal' AND t.isrc IS NOT NULL AND t.isrc != ''")
_CHUNK = 500
log = logging.getLogger("bridge.moods")


def _grouped(conn: sqlite3.Connection, query: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, isrc in conn.execute(query).fetchall():
        out.setdefault(key, set()).add(isrc)
    return out


def live_isrcs(conn: sqlite3.Connection) -> dict[str, set[str]]:
    return _grouped(conn, LIVE_TRACKS)


def open_sound(path: Path) -> sqlite3.Connection | None:
    if not Path(path).is_file():
        return None
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def sound_rows(path: Path, isrcs: set[str]) -> dict[str, tuple[str, str | None, float | None]] | None:
    """isrc -> (status, moods JSON, instrumental) for the given ISRCs; empty while the file or table is missing,
    None when the file is there but cannot be read (the callers then keep what they had)."""
    sound = None
    try:
        sound = open_sound(path)
        if sound is None or sound.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_sound'").fetchone() is None:
            return {}
        wanted = sorted(isrcs)
        out = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            for isrc, status, moods, instrumental in sound.execute(
                    f"SELECT isrc, status, moods, instrumental FROM track_sound "
                    f"WHERE isrc IN ({','.join('?' * len(chunk))})", chunk).fetchall():
                out[isrc] = (status, moods, instrumental)
        return out
    except sqlite3.Error as exc:
        log.warning("sound.sqlite cannot be read (%s: %s); mood footers left as they are", type(exc).__name__, exc)
        return None
    finally:
        if sound is not None:
            sound.close()


def mood_words(scores: dict[str, float]) -> list[str]:
    ranked = sorted(((t, s) for t, s in scores.items() if t not in NOT_MOODS), key=lambda x: (-x[1], x[0]))
    if not ranked:
        return []
    words = [ranked[0][0]]
    if len(ranked) > 1 and ranked[1][1] >= SECOND_WORD_MIN * ranked[0][1]:
        words.append(ranked[1][0])
    return words


def playlist_moods(conn: sqlite3.Connection, sound_path: Path | None) -> dict[str, dict] | None:
    """{playlist name: {"words": [...], "instrumental": 0..1}} for the playlists analysed far enough; None while
    sound.sqlite cannot be read, so a passing read error does not take every footer off and put it back later."""
    return _moods(live_isrcs(conn), sound_path)


def source_moods(conn: sqlite3.Connection, sound_path: Path | None) -> dict[str, dict] | None:
    """The same for the TIDAL playlists of the Discover rows, by playlist uri."""
    return _moods(_grouped(conn, SOURCE_TRACKS), sound_path)


def _moods(playlists: dict[str, set[str]], sound_path: Path | None) -> dict[str, dict] | None:
    if sound_path is None:
        return {}
    rows = sound_rows(Path(sound_path), set().union(*playlists.values()))
    if rows is None:
        return None
    out = {}
    for name, isrcs in sorted(playlists.items()):
        known = [rows[i] for i in isrcs if i in rows]
        analysed = [(json.loads(m), v) for s, m, v in known if s == "done" and m is not None and v is not None]
        if len(known) < MIN_COVERAGE * len(isrcs) or len(analysed) < MIN_DONE:
            continue
        totals: dict[str, float] = {}
        for moods, _ in analysed:
            for tag, score in moods.items():
                totals[tag] = totals.get(tag, 0.0) + score
        words = mood_words({t: s / len(analysed) for t, s in totals.items()})
        if words:
            out[name] = {"words": words, "instrumental": round(sum(v for _, v in analysed) / len(analysed), 2)}
    return out
