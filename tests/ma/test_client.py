import pytest

from bridge.ma.client import MaClient, MaError, tidal_uri
from tests.ma.fake_server import FakeMa


async def test_auth_and_search():
    async with FakeMa() as fake:
        fake.search_results = [{"uri": "tidal--T://track/1"}]
        c = MaClient(fake.url, "good")
        await c.connect()
        assert await c.search_tracks("a b") == [{"uri": "tidal--T://track/1"}]
        assert fake.calls[:2] == ["auth", "music/search"]
        await c.close()


async def test_bad_token_raises_and_does_not_keep_socket():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "bad")
        with pytest.raises(MaError):
            await c.connect()
        assert c._ws is None


async def test_playlist_lifecycle():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("Spotify · Discover Weekly")
        assert await c.find_playlist_by_name("Spotify · Discover Weekly") == pid
        assert await c.replace_playlist(pid, ["tidal--T://track/1", "tidal--T://track/2"], timeout_s=5) == []
        assert await c.replace_playlist(pid, ["tidal--T://track/3"], timeout_s=5) == []
        assert await c.playlist_track_uris(pid) == ["tidal--T://track/3"]
        assert await c.get_playlist("999") is None
        await c.close()


async def test_replace_rebuilds_the_playlist_cover():
    """MA makes a playlist cover from its tracks only every 90 days; the content changes daily."""
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("x")
        await c.replace_playlist(pid, ["tidal--T://track/1"], timeout_s=5)
        assert fake.metadata_refreshed == [(f"library://playlist/{pid}", True)]
        await c.close()


async def test_cover_refresh_failure_does_not_fail_the_publish():
    async with FakeMa() as fake:
        fake.fail_metadata = True
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("x")
        assert await c.replace_playlist(pid, ["tidal--T://track/1"], timeout_s=5) == []
        await c.close()


async def test_replace_reports_missing():
    async with FakeMa() as fake:
        fake.reject = {"tidal--T://track/2"}
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("x")
        missing = await c.replace_playlist(pid, ["tidal--T://track/1", "tidal--T://track/2"], timeout_s=5)
        assert missing == ["tidal--T://track/2"]
        assert fake.playlists[pid] == ["tidal--T://track/1"]
        await c.close()


async def test_replace_waits_for_slow_ma_task(monkeypatch):
    monkeypatch.setattr(MaClient, "TASK_POLL_S", 0.01)
    async with FakeMa() as fake:
        fake.task_delay = 6  # MA takes several polls before the added tracks appear
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("x")
        assert await c.replace_playlist(pid, ["tidal--T://track/1", "tidal--T://track/2"], timeout_s=5) == []
        assert fake.playlists[pid] == ["tidal--T://track/1", "tidal--T://track/2"]
        await c.close()


async def test_replace_raises_when_ma_task_fails(monkeypatch):
    monkeypatch.setattr(MaClient, "TASK_POLL_S", 0.01)
    async with FakeMa() as fake:
        fake.fail_tasks = True
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("x")
        with pytest.raises(MaError, match="provider unavailable"):
            await c.replace_playlist(pid, ["tidal--T://track/1"], timeout_s=5)
        await c.close()


async def test_reconnects_once_after_drop():
    async with FakeMa() as fake:
        fake.search_results = [{"uri": "x"}]
        c = MaClient(fake.url, "good")
        await c.connect()
        fake.drop_next = True
        assert await c.search_tracks("q") == [{"uri": "x"}]
        await c.close()


async def test_no_resend_of_writes():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        await c.connect()
        fake.drop_after_dispatch = True
        with pytest.raises(MaError, match="connection lost"):
            await c.create_playlist("Spotify · Release Radar")
        assert list(fake.names.values()) == ["Spotify · Release Radar"]  # created once, not twice
        await c.close()


async def test_find_playlist_falls_back_to_full_listing():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        pid = await c.create_playlist("Spotify · daylist")
        original = fake._dispatch

        def search_blind(cmd, a):  # simulate MA search that ignores "·"
            if cmd == "music/playlists/library_items" and a.get("search"):
                return []
            return original(cmd, a)

        fake._dispatch = search_blind
        assert await c.find_playlist_by_name("Spotify · daylist") == pid
        await c.close()


def test_tidal_uri_from_library_item():
    track = {"provider": "library", "uri": "library://track/5", "provider_mappings": [
        {"provider_domain": "tidal", "provider_instance": "tidal--test0001", "item_id": "900026000"}]}
    assert tidal_uri(track) == "tidal--test0001://track/900026000"
    assert tidal_uri({"provider": "tidal--test0001", "uri": "tidal--test0001://track/1", "provider_mappings": []}) \
        == "tidal--test0001://track/1"


async def test_capture_reads_are_resent_after_a_drop():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        await c.connect()
        fake.drop_next = True
        assert await c.call("player_queues/get", queue_id="p1") == {"queue_id": "p1"}
        await c.close()


async def test_library_artists_page_through_the_library():
    async with FakeMa() as fake:
        fake.artists = [{"item_id": str(i), "name": f"A{i}"} for i in range(1203)]
        c = MaClient(fake.url, "good")
        got = await c.library_artists()
        assert len(got) == 1203 and got[0] == ("A0", "0") and got[-1] == ("A1202", "1202")
        await c.close()


async def test_refresh_artist_forces_a_metadata_update():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        assert await c.refresh_artist("11") == ""         # the fake returns no bio
        assert fake.metadata_refreshed == [("library://artist/11", True)]
        await c.close()


async def test_add_to_library_by_uri():
    async with FakeMa() as fake:
        c = MaClient(fake.url, "good")
        await c.add_to_library("tidal--T://artist/10")
        assert fake.library_added == ["tidal--T://artist/10"]
        await c.close()


async def test_discover_rows_and_provider_playlist_tracks():
    async with FakeMa() as fake:
        fake.rows = [{"item_id": "mixes", "provider": "tidal--T", "name": "Custom mixes"}]
        fake.row_items[("tidal--T", "mixes")] = [{"uri": "tidal--T://playlist/p1", "media_type": "playlist"}]
        fake.provider_tracks[("tidal--T", "p1")] = [{"uri": "tidal--T://track/1"}]
        c = MaClient(fake.url, "good")
        assert await c.recommendation_rows() == fake.rows
        assert await c.recommendation_items("tidal--T", "mixes") == [{"uri": "tidal--T://playlist/p1",
                                                                      "media_type": "playlist"}]
        assert await c.provider_playlist_tracks("p1", "tidal--T") == [{"uri": "tidal--T://track/1"}]
        await c.close()


async def test_discover_reads_are_resent_after_a_dropped_socket():
    async with FakeMa() as fake:
        fake.rows = [{"item_id": "mixes", "provider": "tidal--T", "name": "Custom mixes"}]
        c = MaClient(fake.url, "good")
        await c.connect()
        fake.drop_next = True
        assert await c.recommendation_rows() == fake.rows
        fake.drop_next = True
        assert await c.recommendation_items("tidal--T", "mixes") == []
        await c.close()
