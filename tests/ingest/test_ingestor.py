from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.ingest.ingestor import Ingestor, latest_tracks
from bridge.models import FetchedPlaylist, SpotifyTrack
from bridge.publish.publisher import ensure_playlist_row
from bridge.resolve.forward import Resolution
from bridge.spotify.client import RateLimited, SpotifyError

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def trk(i):
    return SpotifyTrack(id=f"id{i}", name=f"n{i}", artists=("a",), album="al", duration_ms=1000 * i, explicit=False)


class FakeSpotify:
    def __init__(self, tracks, rate_limit=0, broken=(), no_isrc=()):
        self.tracks, self.meta_calls = tracks, []
        self.rate_limit, self.broken, self.no_isrc = rate_limit, set(broken), set(no_isrc)

    async def fetch_playlist(self, pid):
        return FetchedPlaylist(list(self.tracks), "rev1")

    async def track_metadata(self, tid):
        if self.rate_limit:
            self.rate_limit -= 1
            raise RateLimited(7)
        if tid in self.broken:
            raise SpotifyError("HTTP 500")
        self.meta_calls.append(tid)
        return (None if tid in self.no_isrc else f"ISRC{tid}"), None


class FakeResolver:
    def __init__(self, conn):
        self.conn, self.seen = conn, []

    async def resolve(self, track):
        self.seen.append((track.id, track.isrc))
        uri = f"tidal--T://track/{track.id}"
        self.conn.execute("INSERT INTO track_mapping(direction, spotify_id, ma_provider_uri, method, status, resolved_at) "
                          "VALUES ('forward', ?, ?, 'catalog_isrc', 'matched', ?)", (track.id, uri, NOW.isoformat()))
        return Resolution("matched", uri, "catalog_isrc")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    ensure_playlist_row(c, "discover_weekly", "fixed", "dw")
    return c


async def nosleep(_):
    return None


async def test_first_ingest_snapshots_fetches_isrc_and_resolves(conn):
    sp, res = FakeSpotify([trk(1), trk(2)]), FakeResolver(conn)
    assert await Ingestor(conn, sp, res, lambda: NOW, sleep=nosleep).ingest_slot("discover_weekly", "dw") == "snapshot"
    assert sp.meta_calls == ["id1", "id2"]
    assert res.seen == [("id1", "ISRCid1"), ("id2", "ISRCid2")]
    assert [t.isrc for t in latest_tracks(conn, "discover_weekly")] == ["ISRCid1", "ISRCid2"]
    snap = conn.execute("SELECT revision_id FROM playlist_snapshot").fetchone()
    assert snap["revision_id"] == "rev1"
    row = conn.execute("SELECT last_checked, last_seen FROM playlist WHERE slot='discover_weekly'").fetchone()
    assert row["last_checked"] == NOW.isoformat() and row["last_seen"] is None  # last_seen is the hub's job


async def test_unchanged_playlist_skips_snapshot_and_metadata(conn):
    sp, res = FakeSpotify([trk(1)]), FakeResolver(conn)
    ing = Ingestor(conn, sp, res, lambda: NOW, sleep=nosleep)
    await ing.ingest_slot("discover_weekly", "dw")
    sp.meta_calls.clear()
    res.seen.clear()
    assert await ing.ingest_slot("discover_weekly", "dw") == "unchanged"
    assert sp.meta_calls == [] and res.seen == []
    assert conn.execute("SELECT count(*) FROM playlist_snapshot").fetchone()[0] == 1


async def test_empty_fetch_is_not_accepted(conn):
    ing = Ingestor(conn, FakeSpotify([]), FakeResolver(conn), lambda: NOW, sleep=nosleep)
    assert await ing.ingest_slot("discover_weekly", "dw") == "empty"
    assert conn.execute("SELECT count(*) FROM playlist_snapshot").fetchone()[0] == 0


async def test_rate_limited_track_metadata_waits_and_continues(conn):
    slept = []

    async def sleep(s):
        slept.append(s)

    sp = FakeSpotify([trk(1)], rate_limit=1)
    await Ingestor(conn, sp, FakeResolver(conn), lambda: NOW, sleep=sleep).ingest_slot("discover_weekly", "dw")
    assert 7 in slept and sp.meta_calls == ["id1"]


async def test_backoff_gives_up(conn):
    slept = []

    async def sleep(s):
        slept.append(s)

    sp = FakeSpotify([trk(1)], rate_limit=99)
    with pytest.raises(RateLimited):
        await Ingestor(conn, sp, FakeResolver(conn), lambda: NOW, max_backoff_s=60, sleep=sleep).ingest_slot(
            "discover_weekly", "dw")
    assert sum(s for s in slept if s != 0.5) <= 60


async def test_broken_track_metadata_is_skipped_and_retried_later(conn):
    sp, res = FakeSpotify([trk(1), trk(2)], broken={"id1"}, no_isrc={"id2"}), FakeResolver(conn)
    ing = Ingestor(conn, sp, res, lambda: NOW, sleep=nosleep)
    await ing.ingest_slot("discover_weekly", "dw")
    fetched = dict(conn.execute("SELECT provider_item_id, metadata_fetched FROM source_track").fetchall())
    assert fetched == {"id1": 0, "id2": 1}  # id1 fetch failed -> retried next poll; id2 fetched, has no ISRC
    assert ("id2", None) in res.seen and all(tid != "id1" for tid, _ in res.seen)  # id2 resolves to no_isrc
    assert any("metadata" in p for p in ing.last_problems)


async def test_snapshot_insert_is_atomic(conn, monkeypatch):
    ing = Ingestor(conn, FakeSpotify([trk(1), trk(2)]), FakeResolver(conn), lambda: NOW, sleep=nosleep)
    calls = []
    original = ing._upsert_track

    def flaky(t):
        calls.append(t.id)
        if len(calls) == 2:
            raise RuntimeError("killed mid-insert")
        return original(t)

    monkeypatch.setattr(ing, "_upsert_track", flaky)
    with pytest.raises(RuntimeError):
        await ing.ingest_slot("discover_weekly", "dw")
    assert conn.execute("SELECT count(*) FROM playlist_snapshot").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM playlist_snapshot_item").fetchone()[0] == 0


async def test_metadata_loop_stops_after_three_consecutive_failures(conn):
    sp = FakeSpotify([trk(i) for i in range(1, 11)], broken={f"id{i}" for i in range(1, 11)})
    calls = []
    original = sp.track_metadata

    async def counting(tid):
        calls.append(tid)
        return await original(tid)

    sp.track_metadata = counting
    ing = Ingestor(conn, sp, FakeResolver(conn), lambda: NOW, sleep=nosleep)
    await ing.ingest_slot("discover_weekly", "dw")
    assert len(calls) == 3 and ing.last_problems


async def test_pending_resolutions_are_reported_as_problems(conn):
    class PendingResolver(FakeResolver):
        async def resolve(self, track):
            self.seen.append((track.id, track.isrc))
            return Resolution("pending", reason="ma_error")

    ing = Ingestor(conn, FakeSpotify([trk(1)]), PendingResolver(conn), lambda: NOW, sleep=nosleep)
    await ing.ingest_slot("discover_weekly", "dw")
    assert any("ma_error" in p for p in ing.last_problems)


async def test_short_fetch_held_then_accepted(conn):
    sp = FakeSpotify([trk(i) for i in range(1, 31)])
    ing = Ingestor(conn, sp, FakeResolver(conn), lambda: NOW, sleep=nosleep)
    await ing.ingest_slot("discover_weekly", "dw")
    sp.tracks = [trk(1), trk(2)]
    assert [await ing.ingest_slot("discover_weekly", "dw") for _ in range(3)] == ["held", "held", "snapshot"]
