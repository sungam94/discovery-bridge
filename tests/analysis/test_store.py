import json
import sqlite3
from array import array
from datetime import datetime, timedelta, timezone

import pytest

from analysis.selection import pending
from analysis.store import open_store, save
from tests.analysis.helpers import bridge_db, playlist, source_playlist

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def bridge(tmp_path):
    return bridge_db(tmp_path / "bridge.sqlite")


@pytest.fixture
def sound(tmp_path):
    return open_store(tmp_path / "sound.sqlite")


def test_store_is_in_wal_mode_and_keeps_a_done_row(sound, tmp_path):
    assert sound.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    save(sound, "ISRC1", "done", NOW, moods={"dark": 0.5}, instrumental=0.9, embedding=[0.5, 1.5])
    row = sqlite3.connect(tmp_path / "sound.sqlite").execute(
        "SELECT status, moods, instrumental, embedding, analysed_at, error FROM track_sound WHERE isrc = 'ISRC1'"
    ).fetchone()
    assert row[0] == "done" and json.loads(row[1]) == {"dark": 0.5} and row[2] == 0.9
    assert array("f", row[3]).tolist() == [0.5, 1.5]
    assert row[4] == "2026-10-05T12:00:00+00:00" and row[5] is None


def test_status_is_checked_and_a_new_row_replaces_the_old_one(sound):
    with pytest.raises(sqlite3.IntegrityError):
        save(sound, "ISRC1", "weird", NOW)
    save(sound, "ISRC1", "error", NOW, error="boom")
    save(sound, "ISRC1", "no_preview", NOW)
    assert sound.execute("SELECT status, error FROM track_sound").fetchall() == [("no_preview", None)]


def test_pending_finishes_one_playlist_before_the_next(bridge, sound):
    playlist(bridge, "a", "Spotify · A", ["A1", "A2", "A3"])
    playlist(bridge, "b", "Spotify · B", ["B1", "A1"])
    # the playlist closest to complete comes first; a track shared with it is not repeated later
    assert pending(bridge, sound, NOW) == ["B1", "A1", "A2", "A3"]
    save(sound, "A1", "done", NOW)
    save(sound, "B1", "done", NOW)
    save(sound, "B2", "done", NOW)
    playlist(bridge, "c", "Spotify · C", ["C1", "C2", "C3"])
    assert pending(bridge, sound, NOW) == ["A2", "A3", "C1", "C2", "C3"]
    assert pending(bridge, sound, NOW, limit=1) == ["A2"]


def test_pending_only_takes_live_published_playlists_and_tracks_with_isrc(bridge, sound):
    playlist(bridge, "a", "Spotify · A", ["A1", None])
    playlist(bridge, "s", "Spotify · Stale", ["S1"], stale=1)
    playlist(bridge, "u", "Spotify · Unpublished", ["U1"], published=False)
    playlist(bridge, "n", "Spotify · Not in MA", ["N1"], live=False)
    assert pending(bridge, sound, NOW) == ["A1"]


def test_errors_are_retried_after_seven_days_other_rows_never(bridge, sound):
    playlist(bridge, "a", "Spotify · A", ["E1", "E2", "N1", "D1"])
    save(sound, "E1", "error", NOW - timedelta(days=8), error="x")
    save(sound, "E2", "error", NOW - timedelta(days=6), error="x")
    save(sound, "N1", "no_preview", NOW - timedelta(days=30))
    save(sound, "D1", "done", NOW - timedelta(days=30))
    assert pending(bridge, sound, NOW) == ["E1"]


def test_an_older_sound_file_gets_the_deezer_column(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "s.sqlite")
    old.execute("CREATE TABLE track_sound (isrc TEXT PRIMARY KEY, status TEXT NOT NULL, moods TEXT, "
                "instrumental REAL, embedding BLOB, analysed_at TEXT NOT NULL, error TEXT)")
    old.commit()
    old.close()
    conn = open_store(tmp_path / "s.sqlite")
    assert "deezer" in [r[1] for r in conn.execute("PRAGMA table_info(track_sound)")]


def test_tidal_discover_playlists_come_after_the_own_playlists(bridge, sound):
    source_playlist(bridge, "tidal--a://playlist/1", "tidal", ["T1", "A1", "T2", None])
    source_playlist(bridge, "tidal--a://playlist/2", "tidal", ["T3"])
    source_playlist(bridge, "soundcloud--b://playlist/3", "soundcloud", ["S1"])
    playlist(bridge, "a", "Spotify · A", ["A1", "A2"])
    assert pending(bridge, sound, NOW) == ["A1", "A2", "T3", "T1", "T2"]   # A1 is not repeated
    save(sound, "T3", "done", NOW)
    assert pending(bridge, sound, NOW, limit=3) == ["A1", "A2", "T1"]


def test_a_bridge_database_without_the_discover_tables_is_fine(bridge, sound):
    """The analysis container may read the bridge database before the bridge has added the tables."""
    bridge.execute("DROP TABLE source_playlist_track")
    bridge.execute("DROP TABLE source_playlist")
    playlist(bridge, "a", "Spotify · A", ["A1"])
    assert pending(bridge, sound, NOW) == ["A1"]
