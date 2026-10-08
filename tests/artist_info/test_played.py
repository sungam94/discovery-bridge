from datetime import datetime, timezone

import pytest

from bridge.artist_info.played import PlayedArtists
from bridge.db import connect, migrate
from bridge.ma.client import MaError

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def play(conn, uri, outcome="completed", at="2026-10-05T10:00:00+00:00"):
    conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, started_at, outcome, policy_result) "
                 "VALUES ('ma_play', ?, ?, ?, ?, 'eligible')", (uri, uri, at, outcome))


class FakeMa:
    def __init__(self, tracks, library=(), broken=()):
        self.tracks, self.library, self.broken, self.added = tracks, list(library), set(broken), []

    async def get_item(self, uri):
        return self.tracks[uri]

    async def library_artists(self):
        return self.library

    async def add_to_library(self, uri):
        if uri in self.broken:
            raise MaError("busy")
        self.added.append(uri)


def track(*artists):
    return {"media_type": "track", "artists": [{"name": n, "uri": u, "provider": u.split("://")[0]} for n, u in artists]}


async def test_artists_of_heard_tracks_are_added_once(conn):
    play(conn, "tidal://track/1")
    play(conn, "tidal://track/1", at="2026-10-05T11:00:00+00:00")
    play(conn, "tidal://track/2")
    ma = FakeMa({"tidal://track/1": track(("Hazy Fern", "tidal://artist/10")),
                 "tidal://track/2": track(("Hazy Fern", "tidal://artist/10"), ("Wren", "tidal://artist/11"))})
    w = PlayedArtists(conn, ma, lambda: NOW)
    assert await w.run_once(limit=10) == 2
    assert ma.added == ["tidal://artist/10", "tidal://artist/11"]
    assert await w.run_once(limit=10) == 0 and len(ma.added) == 2


async def test_skips_unheard_tracks_library_artists_and_artists_already_there(conn):
    play(conn, "tidal://track/1", outcome="skipped")
    play(conn, "library://track/2")
    play(conn, "tidal://track/3")
    ma = FakeMa({"library://track/2": track(("Own", "library://artist/5")),
                 "tidal://track/3": track(("Seamoon", "tidal://artist/20"))}, library=[("SeaMoon", "7")])
    assert await PlayedArtists(conn, ma, lambda: NOW).run_once(limit=10) == 0
    assert ma.added == []


async def test_a_failed_add_is_tried_again_later(conn):
    play(conn, "tidal://track/1")
    ma = FakeMa({"tidal://track/1": track(("Hazy Fern", "tidal://artist/10"))}, broken=["tidal://artist/10"])
    w = PlayedArtists(conn, ma, lambda: NOW)
    assert await w.run_once(limit=10) == 0
    ma.broken.clear()
    assert await w.run_once(limit=10) == 1 and ma.added == ["tidal://artist/10"]


async def test_at_most_limit_artists_per_pass(conn):
    for i in range(5):
        play(conn, f"tidal://track/{i}")
    ma = FakeMa({f"tidal://track/{i}": track((f"A{i}", f"tidal://artist/{i}")) for i in range(5)})
    w = PlayedArtists(conn, ma, lambda: NOW)
    assert await w.run_once(limit=2) == 2
    assert await w.run_once(limit=10) == 3
