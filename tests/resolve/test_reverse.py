from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.ma.client import MaError
from bridge.models import SpotifyCandidate
from bridge.resolve.reverse import ReverseResolver, canonical_uri, split_uri
from bridge.spotify.client import AuthExpired, SpotifyError

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
URI = "tidal--T://track/1"
ITEM = {"uri": URI, "provider": "tidal--T", "item_id": "1", "duration": 240, "external_ids": [["isrc", "ISRC1"]],
        "metadata": {"explicit": False}, "album": {"name": "A"}, "provider_mappings": []}


class FakeMa:
    def __init__(self, items=None, fail=None):
        self.items, self.fail, self.calls = items or {URI: ITEM}, fail, []

    async def get_item(self, uri):
        self.calls.append(uri)
        if self.fail:
            raise self.fail
        return self.items[uri]


class FakeSpotify:
    def __init__(self, cands=None, fail=None):
        self.cands = cands if cands is not None else [SpotifyCandidate("sp1", "n", 240_500, False, "A")]
        self.fail, self.calls = fail, []

    async def search_isrc(self, isrc):
        self.calls.append(isrc)
        if self.fail:
            raise self.fail
        return self.cands


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def resolver(conn, ma=None, sp=None, now=NOW):
    return ReverseResolver(conn, ma or FakeMa(), sp or FakeSpotify(), lambda: now)


def test_split_uri():
    assert split_uri("tidal--test0001://track/900026000") == ("tidal--test0001", "900026000")


async def test_canonical_uri_maps_library_items_to_tidal():
    lib = {"uri": "library://track/7", "provider": "library",
           "provider_mappings": [{"provider_domain": "tidal", "provider_instance": "tidal--T", "item_id": "1"}]}
    assert await canonical_uri(FakeMa({"library://track/7": lib}), "library://track/7") == ("tidal--T://track/1", lib)
    assert await canonical_uri(FakeMa(), URI) == (URI, None)


async def test_matches_by_isrc_and_caches(conn):
    sp = FakeSpotify()
    r = resolver(conn, sp=sp)
    assert (await r.resolve(URI)).spotify_id == "sp1"
    assert (await r.resolve(URI)).spotify_id == "sp1"
    assert sp.calls == ["ISRC1"]  # second call answered from track_mapping
    row = conn.execute("SELECT direction, method, status, isrc FROM track_mapping").fetchone()
    assert tuple(row) == ("reverse", "reverse_isrc", "matched", "ISRC1")


async def test_no_isrc_is_unmatched(conn):
    item = {**ITEM, "external_ids": []}
    res = await resolver(conn, ma=FakeMa({URI: item})).resolve(URI)
    assert (res.status, res.reason) == ("unmatched", "no_isrc")


async def test_duration_mismatch_and_no_match(conn):
    res = await resolver(conn, sp=FakeSpotify([SpotifyCandidate("x", "n", 400_000, False, "A")])).resolve(URI)
    assert (res.status, res.reason) == ("unmatched", "duration_mismatch")
    conn.execute("DELETE FROM track_mapping")
    res = await resolver(conn, sp=FakeSpotify([])).resolve(URI)
    assert (res.status, res.reason) == ("unmatched", "no_match")


async def test_unmatched_waits_for_retry_time(conn):
    sp = FakeSpotify([])
    await resolver(conn, sp=sp).resolve(URI)
    await resolver(conn, sp=sp, now=NOW + timedelta(minutes=30)).resolve(URI)
    assert len(sp.calls) == 1
    await resolver(conn, sp=sp, now=NOW + timedelta(hours=2)).resolve(URI)
    assert len(sp.calls) == 2


@pytest.mark.parametrize("err", [MaError("down"), OSError("net"), SpotifyError("5xx"), AuthExpired("cookie")])
async def test_transient_errors_are_pending(conn, err):
    ma_fail = err if isinstance(err, (MaError, OSError)) else None
    sp_fail = None if ma_fail else err
    res = await resolver(conn, ma=FakeMa(fail=ma_fail), sp=FakeSpotify(fail=sp_fail)).resolve(URI)
    assert res.status == "pending"
    assert conn.execute("SELECT count(*) FROM track_mapping").fetchone()[0] == 0


async def test_reverse_override_pin_and_block(conn):
    conn.execute("INSERT INTO manual_override(direction, spotify_id, ma_provider_uri, blocked, created_at) "
                 "VALUES ('reverse', 'pinned', ?, 0, 't')", (URI,))
    sp = FakeSpotify()
    assert (await resolver(conn, sp=sp).resolve(URI)).spotify_id == "pinned"
    assert sp.calls == []
    conn.execute("UPDATE manual_override SET blocked = 1")
    res = await resolver(conn, sp=sp).resolve(URI)
    assert (res.status, res.reason) == ("unmatched", "blocked")
