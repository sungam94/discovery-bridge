from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.genre.store import all_playlist_genres, pending_artists, playlist_complete, playlist_genre_shares, save_artist

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def playlist(conn, slot, name, artists, published=True, stale=0):
    """One track per entry; an entry may hold several artists joined by the unit separator."""
    conn.execute("INSERT INTO playlist(slot, kind, name, ma_playlist_id, stale) VALUES (?, 'fixed', ?, ?, ?)",
                 (slot, name, "7" if published else None, stale))
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                       "VALUES (?, 'x', 't', 'o', 's')", (slot,)).lastrowid
    for pos, a in enumerate(artists):
        tid = conn.execute("INSERT INTO source_track(provider, provider_item_id, artist, title, album, duration_ms) "
                           "VALUES ('spotify', ?, ?, 't', 'al', 1000)", (f"{slot}{pos}", a)).lastrowid
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
    if published:
        conn.execute("UPDATE playlist SET published_snapshot_id = ? WHERE slot = ?", (sid, slot))


def test_specific_genres_beat_umbrella_terms(conn):
    playlist(conn, "dm1", "Spotify · Daily Mix 1", ["Ace Ventura", "Ace Ventura", "Astrix", "Tipper"])
    save_artist(conn, "Ace Ventura", "m1", [("electronic", 9), ("psychedelic", 8), ("psytrance", 7), ("trance", 2)], NOW)
    save_artist(conn, "Astrix", "m2", [("psytrance", 5), ("electronic", 5)], NOW)
    save_artist(conn, "Tipper", "m3", [("breakbeat", 4), ("glitch", 3)], NOW)
    # per track: Ace Ventura twice (psytrance, trance), Astrix (psytrance), Tipper (breakbeat, glitch)
    assert playlist_genre_shares(conn, "Spotify · Daily Mix 1") == [
        ["psytrance", 3], ["trance", 2], ["breakbeat", 1], ["glitch", 1]]
    assert playlist_genre_shares(conn, "Spotify · Daily Mix 1", limit=2) == [["psytrance", 3], ["trance", 2]]


def test_only_umbrella_terms_known_uses_the_strongest_one(conn):
    playlist(conn, "dm2", "Spotify · Daily Mix 2", ["Someone"])
    save_artist(conn, "Someone", "m", [("rock", 3), ("pop", 1)], NOW)
    assert playlist_genre_shares(conn, "Spotify · Daily Mix 2") == [["rock", 1]]


def test_artist_names_match_case_insensitively_and_per_artist_of_a_track(conn):
    playlist(conn, "dm3", "Spotify · Daily Mix 3", ["WILDMOOR\x1fOakmere"])
    save_artist(conn, "Wildmoor", "m", [("black metal", 4)], NOW)
    assert playlist_genre_shares(conn, "Spotify · Daily Mix 3") == [["black metal", 1]]


def test_no_genre_data_gives_an_empty_list(conn):
    playlist(conn, "dm4", "Spotify · Daily Mix 4", ["Unknown"])
    assert playlist_genre_shares(conn, "Spotify · Daily Mix 4") == []
    assert all_playlist_genres(conn) == {}


def test_pending_artists_are_those_of_live_playlists_without_a_fresh_row(conn):
    playlist(conn, "a", "Spotify · A", ["Known", "New One\x1fNew Two", "Old"])
    playlist(conn, "b", "Spotify · B", ["Unpublished"], published=False)
    playlist(conn, "c", "Spotify · C", ["Stale Playlist Artist"], stale=1)
    save_artist(conn, "Known", None, [], NOW - timedelta(days=2))        # looked up, nothing found: not asked again
    save_artist(conn, "Old", "m", [("jazz", 1)], NOW - timedelta(days=40))
    assert sorted(pending_artists(conn, NOW, limit=10)) == ["New One", "New Two", "Old"]
    assert len(pending_artists(conn, NOW, limit=2)) == 2


def test_playlist_is_complete_when_no_artist_is_pending(conn):
    playlist(conn, "a", "Spotify · A", ["One", "Two"])
    save_artist(conn, "One", "m", [("jazz", 1)], NOW)
    assert not playlist_complete(conn, "Spotify · A", NOW)
    save_artist(conn, "Two", None, [], NOW)
    assert playlist_complete(conn, "Spotify · A", NOW)
    assert all_playlist_genres(conn) == {"Spotify · A": [["jazz", 1]]}

