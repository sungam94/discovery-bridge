from datetime import datetime, timedelta, timezone

import pytest

from bridge.capture.source import SourceResolver, bridge_source, snapshot_has
from bridge.db import connect, migrate
from bridge.ma.client import MaError
from tests.capture.helpers import seed_snapshot

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
T1, T2, T9 = "tidal--T://track/1", "tidal--T://track/2", "tidal--T://track/9"


class FakeMa:
    def __init__(self, queue=None, tracks=None, fail=False, items=None):
        self.queue, self.tracks, self.fail, self.calls = queue or {}, tracks or {}, fail, []
        self.items = items or {}

    async def get_item(self, uri):
        return self.items[uri]

    async def call(self, command, **args):
        self.calls.append((command, args))
        if self.fail:
            raise MaError("down")
        if command == "player_queues/get":
            return self.queue
        return self.tracks[(command, args["item_id"])]


def queue(uri, media_type="playlist", autoplay=False):
    return {"sources": [{"uri": uri, "media_type": media_type}], "autoplay_enabled": autoplay,
            "dont_stop_the_music_enabled": False}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def test_bridge_source_current_and_history(conn):
    sid = seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    conn.execute("UPDATE playlist_snapshot SET ma_history_playlist_id = '110' WHERE id = ?", (sid,))
    assert bridge_source(conn, "109") == ("sp_discover_weekly", sid)
    assert bridge_source(conn, "110") == ("sp_discover_weekly", sid)
    assert bridge_source(conn, "999") is None


def test_snapshot_has(conn):
    sid = seed_snapshot(conn, "discover_weekly", "109", [("s1", T1), ("s2", None)])
    assert snapshot_has(conn, sid, T1) and not snapshot_has(conn, sid, T9)


async def test_track_from_bridge_playlist(conn):
    seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    ctx = await SourceResolver(conn, FakeMa(queue("library://playlist/109")), lambda: NOW).context("p1", T1)
    assert ctx == ("109", False)


async def test_autoplay_track_outside_bridge_playlist_is_radio(conn):
    seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    ma = FakeMa(queue("library://playlist/109", autoplay=True))
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T9) == (None, True)


async def test_manually_added_track_has_no_source(conn):
    seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    ma = FakeMa(queue("library://playlist/109", autoplay=False))
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T9) == (None, False)


async def test_album_membership_and_cache(conn):
    tracks = {("music/albums/album_tracks", "5"): [{"uri": T1, "provider": "tidal--T", "provider_mappings": []}]}
    ma = FakeMa(queue("library://album/5", media_type="album", autoplay=True), tracks)
    clock = [NOW]
    src = SourceResolver(conn, ma, lambda: clock[0])
    assert await src.context("p1", T1) == (None, False)
    assert await src.context("p1", T2) == (None, True)
    assert sum(1 for c, _ in ma.calls if c == "music/albums/album_tracks") == 1   # cached
    clock[0] += timedelta(minutes=11)
    await src.context("p1", T1)
    assert sum(1 for c, _ in ma.calls if c == "music/albums/album_tracks") == 2   # cache expired


async def test_provider_album_uri_is_listed_with_its_provider(conn):
    tracks = {("music/albums/album_tracks", "77"): [{"uri": T1, "provider": "tidal--T", "provider_mappings": []}]}
    ma = FakeMa(queue("tidal--T://album/77", media_type="album", autoplay=True), tracks)
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T1) == (None, False)
    assert ma.calls[-1] == ("music/albums/album_tracks", {"item_id": "77", "provider_instance_id_or_domain": "tidal--T"})


async def test_other_source_types_and_no_source(conn):
    ma = FakeMa(queue("tidal--T://artist/3", media_type="artist", autoplay=True))
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T1) == (None, True)
    ma = FakeMa({"sources": [], "autoplay_enabled": False})
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T1) == (None, False)


async def test_ma_error_gives_no_context(conn):
    assert await SourceResolver(conn, FakeMa(fail=True), lambda: NOW).context("p1", T1) == (None, False)


async def test_library_uri_is_matched_by_its_tidal_mapping(conn):
    seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    lib = {"uri": "library://track/7", "provider": "library",
           "provider_mappings": [{"provider_domain": "tidal", "provider_instance": "tidal--T", "item_id": "1"}]}
    ma = FakeMa(queue("library://playlist/109", autoplay=True), items={"library://track/7": lib})
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", "library://track/7") == ("109", False)


async def test_radio_playlist_source_is_radio(conn):
    ma = FakeMa(queue("radio_playlist://track/tidal--T/5", media_type="playlist", autoplay=False))
    assert await SourceResolver(conn, ma, lambda: NOW).context("p1", T1) == (None, True)
