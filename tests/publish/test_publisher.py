from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.ma.client import MaClient
from bridge.publish.publisher import Publisher, PublishIncomplete, ensure_playlist_row, playlist_name
from tests.ma.fake_server import FakeMa

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def add_snapshot(conn, slot, rows, set_hash="s"):
    """rows: list of (spotify_id, status, uri). Returns the snapshot id."""
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                       "VALUES (?, 'pl', ?, 'o', ?)", (slot, NOW.isoformat(), set_hash)).lastrowid
    for pos, (spid, status, uri) in enumerate(rows):
        conn.execute("INSERT OR IGNORE INTO source_track(provider, provider_item_id, artist, title, album, "
                     "duration_ms) VALUES ('spotify', ?, 'a', 't', 'al', 1000)", (spid,))
        tid = conn.execute("SELECT id FROM source_track WHERE provider_item_id=?", (spid,)).fetchone()["id"]
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
        conn.execute("DELETE FROM track_mapping WHERE spotify_id=?", (spid,))
        conn.execute("INSERT INTO track_mapping(direction, spotify_id, ma_provider_uri, status, resolved_at) "
                     "VALUES ('forward', ?, ?, ?, ?)", (spid, uri, status, NOW.isoformat()))
    return sid


def test_playlist_names():
    assert playlist_name("discover_weekly") == "Spotify · Discover Weekly"
    assert playlist_name("daily_mix_3") == "Spotify · Daily Mix 3"
    assert playlist_name("daylist") == "Spotify · daylist"


async def test_publish_creates_and_then_is_unchanged(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "37i9")
        add_snapshot(conn, "discover_weekly", [("a", "matched", "tidal--T://track/1"),
                                               ("b", "unmatched", None), ("c", "matched", "tidal--T://track/3")])
        pub = Publisher(conn, ma, lambda: NOW, tz="Europe/Berlin")
        assert await pub.check("discover_weekly") == "published"
        (pid,) = fake.playlists
        assert fake.names[pid] == "Spotify · Discover Weekly"
        assert fake.playlists[pid] == ["tidal--T://track/1", "tidal--T://track/3"]
        assert await pub.check("discover_weekly") == "unchanged"
        await ma.close()


async def test_publish_dedupes_uris(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "daylist", "daylist", "x")
        add_snapshot(conn, "daylist", [("a", "matched", "tidal--T://track/1"), ("b", "matched", "tidal--T://track/1")])
        await Publisher(conn, ma, lambda: NOW, tz="Europe/Berlin").check("daylist")
        assert list(fake.playlists.values()) == [["tidal--T://track/1"]]
        await ma.close()


async def test_never_publishes_an_empty_playlist(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "daylist", "daylist", "x")
        add_snapshot(conn, "daylist", [("a", "unmatched", None)])
        assert await Publisher(conn, ma, lambda: NOW, tz="Europe/Berlin").check("daylist") == "no_matches"
        assert fake.calls == []
        await ma.close()


async def test_pending_waits_up_to_six_hours(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "release_radar", "fixed", "rr")
        add_snapshot(conn, "release_radar", [("a", "matched", "tidal--T://track/1"), ("b", "pending", None)])
        now = [NOW]
        pub = Publisher(conn, ma, lambda: now[0], tz="Europe/Berlin")
        assert await pub.check("release_radar") == "waiting"
        now[0] = NOW + timedelta(hours=5)
        assert await pub.check("release_radar") == "waiting"
        now[0] = NOW + timedelta(hours=6, minutes=1)
        assert await pub.check("release_radar") == "published"
        await ma.close()


async def test_wait_timer_resets_for_a_new_snapshot(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "release_radar", "fixed", "rr")
        add_snapshot(conn, "release_radar", [("a", "matched", "tidal--T://track/1"), ("b", "pending", None)])
        now = [NOW]
        pub = Publisher(conn, ma, lambda: now[0], tz="Europe/Berlin")
        await pub.check("release_radar")
        now[0] = NOW + timedelta(hours=7)
        add_snapshot(conn, "release_radar", [("c", "matched", "tidal--T://track/3"), ("d", "pending", None)])
        assert await pub.check("release_radar") == "waiting"  # new snapshot -> new 6 h wait
        await ma.close()


async def test_reports_missing_items(conn):
    async with FakeMa() as fake:
        fake.reject = {"tidal--T://track/2"}
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        add_snapshot(conn, "discover_weekly", [("a", "matched", "tidal--T://track/1"), ("b", "matched", "tidal--T://track/2")])
        pub = Publisher(conn, ma, lambda: NOW, tz="Europe/Berlin")
        assert await pub.check("discover_weekly") == "published (1 missing)"
        assert await pub.check("discover_weekly") == "unchanged"  # no hourly re-publish loop
        await ma.close()


async def test_mostly_missing_publish_is_an_error_and_retried(conn):
    async with FakeMa() as fake:
        fake.reject = {"tidal--T://track/1", "tidal--T://track/2"}
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        add_snapshot(conn, "discover_weekly", [("a", "matched", "tidal--T://track/1"), ("b", "matched", "tidal--T://track/2")])
        pub = Publisher(conn, ma, lambda: NOW, publish_history={"discover_weekly"}, tz="Europe/Berlin")
        with pytest.raises(PublishIncomplete, match="2 of 2"):
            await pub.check("discover_weekly")
        row = conn.execute("SELECT published_items_hash, history_set_hash FROM playlist WHERE slot='discover_weekly'").fetchone()
        assert row["published_items_hash"] is None and row["history_set_hash"] is None  # not recorded, no history yet
        fake.reject = set()
        assert await pub.check("discover_weekly") == "published"  # next poll retries
        await ma.close()


async def test_recreates_deleted_playlist(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "37i9")
        add_snapshot(conn, "discover_weekly", [("a", "matched", "tidal--T://track/1")])
        pub = Publisher(conn, ma, lambda: NOW, tz="Europe/Berlin")
        await pub.check("discover_weekly")
        fake.playlists.clear()
        fake.names.clear()
        assert await pub.check("discover_weekly") == "published"
        assert list(fake.playlists.values()) == [["tidal--T://track/1"]]
        await ma.close()


async def test_no_snapshot(conn):
    async with FakeMa() as fake:
        ensure_playlist_row(conn, "daylist", "daylist", None)
        assert await Publisher(conn, MaClient(fake.url, "good"), lambda: NOW, tz="Europe/Berlin").check("daylist") == "no_snapshot"


def test_stored_spotify_id_survives_a_none(conn):
    ensure_playlist_row(conn, "discover_weekly", "fixed", "37i9dQZEVXcFakeDw00001")
    ensure_playlist_row(conn, "discover_weekly", "fixed", None)
    row = conn.execute("SELECT spotify_id FROM playlist WHERE slot = 'discover_weekly'").fetchone()
    assert row["spotify_id"] == "37i9dQZEVXcFakeDw00001"
