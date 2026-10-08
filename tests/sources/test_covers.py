import json
from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.genre.store import save_artist
from bridge.sources.covers import source_entries
from tests.genre.test_source_genres import source

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def test_artist_genres_are_read_once_for_all_playlists(conn):
    for i in range(4):
        source(conn, f"tidal--a://playlist/{i}", "tidal", [("Astrix\x1fZed", []), ("Wren", [])], name=f"Mix {i}")
    save_artist(conn, "Astrix", "m1", [("psytrance", 5)], NOW)
    save_artist(conn, "Zed", "m2", [("zzqx", 5)], NOW)          # a genre the wheel cannot place
    save_artist(conn, "Wren", "m3", [("sludge metal", 3)], NOW)
    statements = []
    conn.set_trace_callback(statements.append)
    entries, hues = source_entries(conn, NOW, {}, {})
    conn.set_trace_callback(None)
    assert entries["tidal--a://playlist/3"] == {"name": "Mix 3", "source": "tidal",
                                                "genres": [["psytrance", 1], ["sludge metal", 1]]}
    assert len(entries) == 4 and set(hues) == {"psytrance", "sludge metal"}
    scans = [s for s in statements if "FROM artist_genre" in s]
    assert sum("fetched_at >" in s for s in scans) == 1
    assert sum("WHERE artist_key =" in s for s in scans) == 3         # once per artist
    assert sum("genres != '[]'" in s for s in scans) <= 1          # the unplaced genre is looked for once
    assert sum("image IS NOT NULL" in s for s in statements) <= 1    # album covers: one read for all playlists


def test_a_tidal_entry_lists_the_first_six_different_album_covers_of_its_tracks(conn):
    source(conn, "tidal--a://playlist/1", "tidal", [("Astrix", [])] * 9)
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [("dj", ["Darkpsy"])] * 2, name="Your Mix")
    pics = ["a", "b", "a", None, "c", "d", "e", "f", "g"]          # a repeated album counts once
    for pos, pic in enumerate(pics):
        conn.execute("UPDATE source_playlist_track SET image = ? WHERE playlist_uri = ? AND position = ?",
                     (json.dumps([f"https://t/{pic}.jpg", "tidal--a"]) if pic else None, "tidal--a://playlist/1", pos))
    conn.execute("UPDATE source_playlist_track SET image = ? WHERE playlist_uri = 'soundcloud--b://playlist/2'",
                 (json.dumps(["https://sc/x.jpg", "soundcloud--b"]),))
    save_artist(conn, "Astrix", "m1", [("psytrance", 5)], NOW)
    entries, _ = source_entries(conn, NOW, {}, {})
    assert entries["tidal--a://playlist/1"]["tiles"] == [[f"https://t/{p}.jpg", "tidal--a"] for p in "abcdef"]
    assert "tiles" not in entries["soundcloud--b://playlist/2"]           # SoundCloud keeps its own artwork
