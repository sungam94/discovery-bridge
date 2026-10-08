import json
from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.genre.store import pending_artists, save_artist, source_complete, source_genre_shares
from tests.genre.test_store import playlist

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def source(conn, uri, kind, tracks, name="My Mix 1"):
    """tracks: (artists joined by the unit separator, genre tags) per track."""
    conn.execute("INSERT INTO source_playlist(uri, name, source, row_name, fetched_at) VALUES (?, ?, ?, 'r', 't')",
                 (uri, name, kind))
    for pos, (artists, tags) in enumerate(tracks):
        conn.execute("INSERT INTO source_playlist_track(playlist_uri, position, track_uri, artists, genres) "
                     "VALUES (?, ?, ?, ?, ?)", (uri, pos, f"{uri}/t{pos}", artists, json.dumps(tags)))


def test_tidal_artists_are_looked_up_after_the_own_playlists(conn):
    playlist(conn, "dw", "Spotify · Discover Weekly", ["Own"])
    source(conn, "tidal--a://playlist/1", "tidal", [("Hazy Fern\x1fOwn", []), ("Wren", [])])
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [("dj uploader", ["Darkpsy"])])
    assert pending_artists(conn, NOW, 10) == ["Own", "Hazy Fern", "Wren"]   # SoundCloud uploaders: never
    assert pending_artists(conn, NOW, 2) == ["Own", "Hazy Fern"]


def test_tidal_playlist_genres_come_from_its_artists(conn):
    source(conn, "tidal--a://playlist/1", "tidal", [("Ace Ventura", []), ("Astrix", []), ("Tipper", [])])
    save_artist(conn, "Ace Ventura", "m1", [("electronic", 9), ("psytrance", 7)], NOW)
    save_artist(conn, "Astrix", "m2", [("psytrance", 5)], NOW)
    assert not source_complete(conn, "tidal--a://playlist/1", NOW)
    save_artist(conn, "Tipper", "m3", [("breakbeat", 4)], NOW)
    assert source_complete(conn, "tidal--a://playlist/1", NOW)
    assert not source_complete(conn, "tidal--a://playlist/1", NOW + timedelta(days=31))
    assert source_genre_shares(conn, "tidal--a://playlist/1") == [["psytrance", 2], ["breakbeat", 1]]


def test_soundcloud_genres_are_the_tags_the_wheel_knows_by_name(conn):
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [
        ("x", ["Darkpsy", "darkpsy"]), ("y", ["experimental  Psytrance", "Sectio Aurea RMX"]),
        ("z", ["DARKPSY"]), ("w", [])])
    assert source_complete(conn, "soundcloud--b://playlist/2", NOW)
    assert source_genre_shares(conn, "soundcloud--b://playlist/2") == [["darkpsy", 2], ["experimental psytrance", 1]]


def test_a_playlist_whose_tracks_were_never_read_is_not_complete(conn):
    source(conn, "tidal--a://playlist/1", "tidal", [])
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [])
    assert not source_complete(conn, "tidal--a://playlist/1", NOW)
    assert not source_complete(conn, "soundcloud--b://playlist/2", NOW)


def test_at_most_the_limit_of_genres(conn):
    tags = ["techno", "house", "trance", "dub", "jazz", "funk", "soul", "punk"]
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [(str(n), [t]) for n, t in enumerate(tags)])
    assert len(source_genre_shares(conn, "soundcloud--b://playlist/2", limit=6)) == 6
    assert len(source_genre_shares(conn, "soundcloud--b://playlist/2", limit=None)) == 8
    assert source_genre_shares(conn, "unknown://playlist/3") == []


def test_soundcloud_broad_tags_are_left_out_when_the_playlist_has_specific_ones(conn):
    source(conn, "soundcloud--b://playlist/2", "soundcloud", [   # SoundCloud tracks carry one tag each
        ("a", ["Electronic"]), ("b", ["electronic"]), ("c", ["Electronic"]), ("d", ["Darkpsy"]), ("e", ["Psycore"])])
    assert source_genre_shares(conn, "soundcloud--b://playlist/2") == [["darkpsy", 1], ["psycore", 1]]
    source(conn, "soundcloud--b://playlist/3", "soundcloud", [("a", ["Electronic"]), ("b", ["Rock"])], name="Broad")
    assert source_genre_shares(conn, "soundcloud--b://playlist/3") == [["electronic", 1], ["rock", 1]]
