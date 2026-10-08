from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.ma.client import MaClient
from bridge.publish.publisher import Publisher, ensure_playlist_row, history_base_name
from tests.ma.fake_server import FakeMa
from tests.publish.test_publisher import add_snapshot

NOW = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)  # 06:00 Berlin, Monday
DW = frozenset({"discover_weekly"})


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def snap(conn, rows, set_hash):
    sid = add_snapshot(conn, "discover_weekly", rows, set_hash=set_hash)
    conn.execute("UPDATE playlist_snapshot SET fetched_at = ? WHERE id = ?", (NOW.isoformat(), sid))
    return sid


def test_history_name_uses_local_date():
    assert history_base_name("discover_weekly", "2026-09-27T22:30:00+00:00", "Europe/Berlin") == "Spotify · DW 2026-09-28"
    assert history_base_name("release_radar", "2026-10-02T06:00:00+00:00", "Europe/Berlin") == "Spotify · RR 2026-10-02"


async def test_history_created_once_per_track_set(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        snap(conn, [("a", "matched", "tidal--T://track/1")], "S1")
        pub = Publisher(conn, ma, lambda: NOW, publish_history=DW, tz="Europe/Berlin")
        await pub.check("discover_weekly")
        await pub.check("discover_weekly")
        assert sorted(fake.names.values()) == ["Spotify · DW 2026-09-28", "Spotify · Discover Weekly"]
        hist = next(pid for pid, n in fake.names.items() if n.startswith("Spotify · DW"))
        assert fake.playlists[hist] == ["tidal--T://track/1"]
        await ma.close()


async def test_second_set_same_day_gets_suffix(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        pub = Publisher(conn, ma, lambda: NOW, publish_history=DW, tz="Europe/Berlin")
        snap(conn, [("a", "matched", "tidal--T://track/1")], "S1")
        await pub.check("discover_weekly")
        snap(conn, [("b", "matched", "tidal--T://track/2")], "S2")
        await pub.check("discover_weekly")
        assert "Spotify · DW 2026-09-28-2" in fake.names.values()
        await ma.close()


async def test_existing_ma_playlist_with_same_name_is_not_overwritten(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        user_pid = await ma.create_playlist("Spotify · DW 2026-09-28")  # e.g. left over from a restored DB
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        snap(conn, [("a", "matched", "tidal--T://track/1")], "S1")
        await Publisher(conn, ma, lambda: NOW, publish_history=DW, tz="Europe/Berlin").check("discover_weekly")
        assert fake.playlists[user_pid] == []
        assert "Spotify · DW 2026-09-28-2" in fake.names.values()
        await ma.close()


async def test_no_history_while_tracks_pending(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        snap(conn, [("a", "matched", "tidal--T://track/1"), ("b", "pending", None)], "S1")
        await Publisher(conn, ma, lambda: NOW, publish_history=DW, tz="Europe/Berlin").check("discover_weekly")
        assert not any(n.startswith("Spotify · DW") for n in fake.names.values())
        await ma.close()


async def test_no_history_for_daily_mix(conn):
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "daily_mix_1", "daily_mix", "m1")
        add_snapshot(conn, "daily_mix_1", [("a", "matched", "tidal--T://track/1")])
        await Publisher(conn, ma, lambda: NOW, publish_history=DW, tz="Europe/Berlin").check("daily_mix_1")
        assert list(fake.names.values()) == ["Spotify · Daily Mix 1"]
        await ma.close()


async def test_publisher_names_history_by_the_berlin_day(conn):
    late = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)  # 01:30 on the 28th in Berlin
    async with FakeMa() as fake:
        ma = MaClient(fake.url, "good")
        ensure_playlist_row(conn, "discover_weekly", "fixed", "dw")
        sid = add_snapshot(conn, "discover_weekly", [("a", "matched", "tidal--T://track/1")], set_hash="S1")
        conn.execute("UPDATE playlist_snapshot SET fetched_at = ? WHERE id = ?", (late.isoformat(), sid))
        await Publisher(conn, ma, lambda: late, publish_history=DW, tz="Europe/Berlin").check("discover_weekly")
        assert "Spotify · DW 2026-09-28" in fake.names.values()
        await ma.close()
