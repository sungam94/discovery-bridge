"""Discover row layout for the MA `spotify_bridge` provider: which published playlists go in which row.

The bridge writes `layout.json` into a directory that MA sees inside the provider folder; the provider
reads it on every request. Only published, non-stale playlists are listed, by their MA playlist name.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from bridge.genre.store import all_playlist_genres
from bridge.genre.wheel import hues_for
from bridge.publish.moods import playlist_moods, source_moods
from bridge.sources.covers import source_entries
from bridge.texts import check_language, has_text, text
from bridge.timeutil import utcnow

LAYOUT_VERSION = 1
SECTION_JUST_FOR_YOU = "spotify:section:0JQ5DACFo5h0jxzOyHOsIe"
_LIVE = "ma_playlist_id IS NOT NULL AND stale = 0"
SUBTITLE_ARTISTS = 3


def _names(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[str]:
    return [r["name"] for r in conn.execute(f"SELECT name FROM playlist WHERE {_LIVE} AND {where}", params)]


def _subtitle(conn: sqlite3.Connection, name: str) -> str | None:
    """Card subtitle for MA (shown as the playlist owner): the most frequent artists of the published snapshot."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for r in conn.execute(
            "SELECT t.artist FROM playlist p JOIN playlist_snapshot_item i ON i.snapshot_id = p.published_snapshot_id "
            "JOIN source_track t ON t.id = i.source_track_id WHERE p.name = ? ORDER BY i.position", (name,)):
        for artist in filter(None, (a.strip() for a in r["artist"].split("\x1f"))):
            counts[artist] += 1
            first_seen.setdefault(artist, len(first_seen))
    top = sorted(counts, key=lambda a: (-counts[a], first_seen[a]))[:SUBTITLE_ARTISTS]
    return ", ".join(top) or None


def _title(language: str, row_id: str) -> str:
    """Row titles live in bridge/texts.py, by row id; a hub section without its own title gets a generic one."""
    return text(language, row_id if has_text(language, row_id) else "section_other")


def build_layout(conn: sqlite3.Connection, hub_sections: tuple[str, ...], *, language: str) -> list[dict]:
    check_language(language)
    rows = [
        ("for_you", _title(language, "for_you"),  # new music and the "just for you" section share one row
         _names(conn, "slot IN ('discover_weekly', 'release_radar') ORDER BY slot")
         + _names(conn, "slot = 'daylist'")
         + _names(conn, "kind = 'hub' AND section = ? ORDER BY hub_position", (SECTION_JUST_FOR_YOU,))),
        ("daily_mixes", _title(language, "daily_mixes"), _names(conn, "kind = 'daily_mix' ORDER BY slot")),
    ]
    for sec in hub_sections:
        if sec != SECTION_JUST_FOR_YOU:
            row_id = f"section_{sec.rsplit(':', 1)[1]}"
            rows.append((row_id, _title(language, row_id),
                         _names(conn, "kind = 'hub' AND section = ? ORDER BY hub_position", (sec,))))
    out = []
    for i, t, names in rows:
        if names:
            subtitles = {n: s for n in names if (s := _subtitle(conn, n))}
            out.append({"id": i, "title": t, "playlists": names, "subtitles": subtitles})
    return out


def _last(path: Path, key: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _last_moods(path: Path) -> dict:
    return _last(path, "moods")


def _last_source_moods(path: Path) -> dict:
    return {uri: e["moods"] for uri, e in _last(path, "sources").items() if isinstance(e, dict) and e.get("moods")}


def layout_sources(conn: sqlite3.Connection, now: datetime, hue_overrides: dict | None, sound_path: Path | None,
                   directory: Path) -> tuple[dict[str, dict], dict[str, float]]:
    """The "sources" entries (TIDAL and SoundCloud Discover playlists) and the hues of their genres; while
    sound.sqlite cannot be read, the moods of the last layout file stay."""
    moods = source_moods(conn, sound_path)
    if moods is None:
        moods = _last_source_moods(Path(directory) / "layout.json")
    return source_entries(conn, now, hue_overrides, moods)


def write_layout(conn: sqlite3.Connection, hub_sections: tuple[str, ...], directory: Path,
                 cover_style: dict | None = None, hue_overrides: dict | None = None,
                 sound_path: Path | None = None, now: datetime | None = None, *, language: str) -> None:
    """Replace layout.json atomically, so MA never reads a half-written file. sound_path is the sound
    analysis database (sound.sqlite) the mood footers come from."""
    directory = Path(directory)
    now = now or utcnow()
    moods = playlist_moods(conn, sound_path)
    if moods is None:  # sound.sqlite cannot be read just now: keep the moods of the last layout file
        moods = _last_moods(directory / "layout.json")
    genres = all_playlist_genres(conn)
    hues = hues_for(conn, sorted({g for shares in genres.values() for g, _ in shares}), hue_overrides or {}, now)
    sources, source_hues = layout_sources(conn, now, hue_overrides, sound_path, directory)
    hues = {**{g: h for g, h in hues.items() if h is not None}, **source_hues}
    rows = build_layout(conn, hub_sections, language=language)
    data = json.dumps({"version": LAYOUT_VERSION, "rows": rows, "genres": genres,
                       "hues": dict(sorted(hues.items())), "cover_style": cover_style or {}, "moods": moods,
                       "sources": sources},
                      ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".layout-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / "layout.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
