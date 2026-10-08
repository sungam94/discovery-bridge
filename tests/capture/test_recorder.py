from datetime import datetime, timedelta, timezone

import pytest

from bridge.capture.plays import ClosedPlay, Play
from bridge.capture.recorder import PlayerDirectory, Recorder, forward_spotify_id
from bridge.config import Thresholds
from bridge.db import connect, migrate
from bridge.ma.client import MaError
from bridge.repo import set_setting
from bridge.resolve.reverse import ReverseResult
from tests.capture.helpers import seed_snapshot

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
T1, T9 = "tidal--T://track/1", "tidal--T://track/9"


class FakePlayers:
    def __init__(self, result=None):
        self.result = result

    async def check(self, player_id):
        return self.result


class FakeReverse:
    def __init__(self, result=ReverseResult("matched", "rev1")):
        self.result, self.calls = result, []

    async def resolve(self, uri):
        self.calls.append(uri)
        return self.result


class FakeMa:
    def __init__(self, items=None, players=None, fail=False):
        self.items, self.players, self.fail = items or {}, players, fail

    async def get_item(self, uri):
        return self.items[uri]

    async def call(self, command, **args):
        if self.fail:
            raise MaError("down")
        assert command == "players/all"
        return self.players


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def closed(uri=T1, secs=240.0, duration_ms=240_000, ended_by="next", player="p1", media_type="track",
           source=None, radio=False, started=NOW - timedelta(minutes=4)):
    play = Play(player, uri, media_type, "u1", duration_ms, started, secs, secs * 1000 >= (duration_ms or 1),
                False, source, radio, player)
    return ClosedPlay(play, ended_by)


def recorder(conn, players=None, reverse=None, ma=None):
    return Recorder(conn, ma or FakeMa(), players or FakePlayers(), reverse or FakeReverse(), Thresholds(),
                    lambda: NOW)


def event(conn):
    return conn.execute("SELECT * FROM taste_event ORDER BY id DESC LIMIT 1").fetchone()


async def test_completed_play_from_bridge_playlist(conn):
    sid = seed_snapshot(conn, "discover_weekly", "109", [("s1", T1)])
    rev = FakeReverse()
    assert await recorder(conn, reverse=rev).record_play(closed(source="109")) == "feedback_paused"
    e = event(conn)
    assert (e["outcome"], e["spotify_id"], e["source_spotify_playlist_id"], e["source_snapshot_id"]) == \
        ("completed", "s1", "sp_discover_weekly", sid)
    assert (e["provider"], e["provider_item_id"], e["listened_ms"], e["isrc"]) == ("tidal--T", "1", 240_000, "ISRC_s1")
    assert rev.calls == []  # forward mapping was enough


async def test_ambiguous_forward_mapping_prefers_source_snapshot(conn):
    seed_snapshot(conn, "release_radar", "111", [("s_old", T1)], fetched_at="2026-10-01T00:00:00+00:00")
    sid = seed_snapshot(conn, "discover_weekly", "109", [("s_dw", T1)], fetched_at="2026-09-01T00:00:00+00:00")
    assert forward_spotify_id(conn, T1, sid)[0] == "s_dw"
    assert forward_spotify_id(conn, T1, None)[0] == "s_old"  # most recent resolved_at


async def test_not_paused_is_eligible(conn):
    set_setting(conn, "feedback_paused", "false")
    assert await recorder(conn).record_play(closed()) == "eligible"
    assert event(conn)["spotify_id"] == "rev1"


async def test_skip_is_no_job_without_reverse_search(conn):
    rev = FakeReverse()
    assert await recorder(conn, reverse=rev).record_play(closed(secs=10)) == "no_job"
    assert event(conn)["outcome"] == "skipped" and rev.calls == []


@pytest.mark.parametrize("kwargs, players, expected", [
    ({"duration_ms": None}, None, "no_duration"),
    ({}, "player_not_allowed", "player_not_allowed"),
    ({}, "player_unknown", "player_unknown"),
    ({"media_type": "radio"}, None, "not_a_track"),
    ({"radio": True}, None, "radio"),
])
async def test_filters(conn, kwargs, players, expected):
    assert await recorder(conn, players=FakePlayers(players)).record_play(closed(**kwargs)) == expected


async def test_reverse_pending_then_retry(conn):
    rev = FakeReverse(ReverseResult("pending"))
    rec = recorder(conn, reverse=rev)
    assert await rec.record_play(closed(uri=T9)) == "pending_resolve"
    rev.result = ReverseResult("matched", "sp9")
    assert await rec.retry_pending() == 1
    e = event(conn)
    assert (e["policy_result"], e["spotify_id"]) == ("feedback_paused", "sp9")
    assert rev.calls == [T9, T9]


async def test_reverse_unmatched_is_unresolved(conn):
    rev = FakeReverse(ReverseResult("unmatched", reason="no_isrc"))
    assert await recorder(conn, reverse=rev).record_play(closed(uri=T9)) == "unresolved"


async def test_duplicate_delivery_is_dropped(conn):
    rec = recorder(conn)
    await rec.record_play(closed())
    await rec.record_play(closed())
    assert conn.execute("SELECT count(*) FROM taste_event").fetchone()[0] == 1


async def test_library_uri_is_stored_under_its_tidal_key(conn):
    lib = {"uri": "library://track/7", "provider": "library",
           "provider_mappings": [{"provider_domain": "tidal", "provider_instance": "tidal--T", "item_id": "1"}]}
    await recorder(conn, ma=FakeMa({"library://track/7": lib})).record_play(closed(uri="library://track/7"))
    e = event(conn)
    assert (e["ma_item_uri"], e["provider"], e["provider_item_id"]) == ("library://track/7", "tidal--T", "1")


async def test_favorite_bypasses_player_filter(conn):
    lib = {"uri": "library://track/7", "provider": "library",
           "provider_mappings": [{"provider_domain": "tidal", "provider_instance": "tidal--T", "item_id": "1"}]}
    rec = recorder(conn, players=FakePlayers("player_not_allowed"), ma=FakeMa({"library://track/7": lib}))
    assert await rec.record_favorite("library://track/7", NOW) == "feedback_paused"
    e = event(conn)
    assert (e["source"], e["outcome"], e["spotify_id"], e["player_ids"]) == ("ma_favorite", "favorite", "rev1", None)


async def test_player_directory():
    players = [{"player_id": "a", "group_members": []}, {"player_id": "g", "group_members": ["a", "b"]},
               {"player_id": "b", "group_members": []}]
    d = PlayerDirectory(FakeMa(players=players), frozenset({"a", "g"}), lambda: NOW)
    assert await d.check("a") is None
    assert await d.check("b") == "player_not_allowed"
    assert await d.check("g") == "player_not_allowed"  # member b is not allowed
    assert await d.check("zz") == "player_unknown"
    d2 = PlayerDirectory(FakeMa(players=players), frozenset({"a", "b", "g"}), lambda: NOW)
    assert await d2.check("g") is None


async def test_player_directory_without_ma():
    d = PlayerDirectory(FakeMa(fail=True), frozenset({"a"}), lambda: NOW)
    assert await d.check("a") is None
    assert await d.check("b") == "player_not_allowed"


async def test_unresolved_events_are_retried(conn):
    rev = FakeReverse(ReverseResult("unmatched", reason="no_match"))
    rec = recorder(conn, reverse=rev)
    assert await rec.record_play(closed(uri=T9)) == "unresolved"
    rev.result = ReverseResult("matched", "sp9")
    assert await rec.retry_pending() == 1
    assert (event(conn)["policy_result"], event(conn)["spotify_id"]) == ("feedback_paused", "sp9")


async def test_retry_stops_at_the_first_pending_result(conn):
    rev = FakeReverse(ReverseResult("pending"))
    rec = recorder(conn, reverse=rev)
    await rec.record_play(closed(uri=T9))
    await rec.record_play(closed(uri="tidal--T://track/8"))
    rev.calls.clear()
    assert await rec.retry_pending() == 0
    assert len(rev.calls) == 1  # an outage affects every row: one attempt per round
