from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from websockets.exceptions import InvalidHandshake

from bridge.db import connect, migrate
from bridge.ma.client import MaError
from bridge.models import SpotifyTrack
from bridge.resolve.forward import ForwardResolver, needs_resolution, next_retry

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = SpotifyTrack(id="0FakeTrack000000000001", name="A Low Tide", artists=("Mire Of Dusk",), album="A Low Tide",
                 duration_ms=357122, explicit=False, isrc="QZFAK2600001")


class FakeMa:
    def __init__(self, results, fail=None, only_query_with=None):
        self.results, self.fail, self.only, self.queries = results, fail, only_query_with, []

    async def search_tracks(self, query, limit=25):
        self.queries.append(query)
        if self.fail:
            raise self.fail
        if self.only and self.only not in query:
            return []
        return self.results

    async def get_item(self, uri):
        return {"uri": uri}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


async def test_matches_by_isrc(conn, fixture):
    ma = FakeMa(fixture("check06_search")["tracks"])
    r = await ForwardResolver(conn, ma, lambda: NOW).resolve(T)
    assert (r.status, r.ma_uri, r.method) == ("matched", "tidal--test0001://track/900026000", "catalog_isrc")
    assert ma.queries == ["Mire Of Dusk A Low Tide"]
    r2 = await ForwardResolver(conn, ma, lambda: NOW).resolve(T)
    assert r2.ma_uri == r.ma_uri and len(ma.queries) == 1  # cached


async def test_second_query_with_album(conn, fixture):
    ma = FakeMa(fixture("check06_search")["tracks"], only_query_with="Mire Of Dusk A Low Tide A Low Tide")
    r = await ForwardResolver(conn, ma, lambda: NOW).resolve(T)
    assert r.status == "matched" and ma.queries == ["Mire Of Dusk A Low Tide", "Mire Of Dusk A Low Tide A Low Tide"]


async def test_no_isrc_is_unmatched(conn):
    r = await ForwardResolver(conn, FakeMa([]), lambda: NOW).resolve(replace(T, isrc=None))
    assert (r.status, r.reason) == ("unmatched", "no_isrc")


async def test_no_duration_is_unmatched(conn):
    r = await ForwardResolver(conn, FakeMa([]), lambda: NOW).resolve(replace(T, duration_ms=0))
    assert (r.status, r.reason) == ("unmatched", "no_duration")


async def test_no_match_schedules_retry(conn):
    r = await ForwardResolver(conn, FakeMa([]), lambda: NOW).resolve(T)
    assert (r.status, r.reason) == ("unmatched", "no_match")
    row = conn.execute("SELECT retry_count, unmatched_retry_at FROM track_mapping").fetchone()
    assert row["retry_count"] == 1
    assert row["unmatched_retry_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert needs_resolution(conn, T.id, NOW) is False
    assert needs_resolution(conn, T.id, NOW + timedelta(hours=2)) is True


async def test_duration_mismatch(conn, fixture):
    results = fixture("check06_search")["tracks"]
    for t in results:
        t["duration"] = 999
    r = await ForwardResolver(conn, FakeMa(results), lambda: NOW).resolve(T)
    assert r.reason == "duration_mismatch"


@pytest.mark.parametrize("err", [MaError("timeout", 16), OSError("reset"), InvalidHandshake("503")])
async def test_transient_error_is_pending(conn, err):
    r = await ForwardResolver(conn, FakeMa([], fail=err), lambda: NOW).resolve(T)
    assert r.status == "pending"
    assert needs_resolution(conn, T.id, NOW) is True


async def test_override_pin_and_block(conn):
    conn.execute("INSERT INTO manual_override(direction, spotify_id, ma_provider_uri, blocked, created_at) "
                 "VALUES ('forward', ?, 'tidal--test0001://track/1', 0, ?)", (T.id, NOW.isoformat()))
    r = await ForwardResolver(conn, FakeMa([]), lambda: NOW).resolve(T)
    assert (r.status, r.ma_uri, r.method) == ("matched", "tidal--test0001://track/1", "override")
    conn.execute("UPDATE manual_override SET blocked = 1, ma_provider_uri = NULL")
    conn.execute("UPDATE track_mapping SET status = 'invalidated'")
    r = await ForwardResolver(conn, FakeMa([]), lambda: NOW).resolve(T)
    assert (r.status, r.reason) == ("unmatched", "blocked")


def test_retry_schedule():
    assert [next_retry(n, NOW) - NOW for n in range(5)] == [
        timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), timedelta(days=7), timedelta(days=7)]
