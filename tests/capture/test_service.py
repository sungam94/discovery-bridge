from datetime import datetime, timedelta, timezone

import pytest

from bridge.capture.plays import PlayTracker
from bridge.capture.service import CaptureService
from bridge.db import connect, migrate

T0 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeSource:
    async def context(self, queue_id, uri):
        return "109", False


class FakeRecorder:
    def __init__(self):
        self.plays, self.favorites, self.retries = [], [], 0

    async def record_play(self, c):
        self.plays.append(c)
        return "feedback_paused"

    async def record_favorite(self, uri, flagged_at):
        self.favorites.append(uri)
        return "feedback_paused"

    async def retry_pending(self):
        self.retries += 1
        return 0


class FakeFavorites:
    def __init__(self):
        self.loaded, self.updates, self.sync = None, [], []
        self.due = []

    def load(self, uris):
        self.loaded = list(uris)

    def on_item_updated(self, data):
        self.updates.append(data["uri"])

    def on_tasks_updated(self, data):
        self.sync.append(bool(data))

    def on_sync_completed(self):
        self.sync.append("done")

    def release_due(self):
        due, self.due = self.due, []
        return due


class FakeMa:
    async def call(self, command, **args):
        assert command == "music/tracks/library_items"
        return [{"uri": "library://track/1"}] if args.get("offset", 0) == 0 else []


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def played(uri, secs=0, player="p1"):
    return {"event": "media_item_played", "data": {"uri": uri, "media_type": "track", "duration": 240,
                                                   "seconds_played": secs, "fully_played": False,
                                                   "player_id": player, "userid": "u"}}


def service(conn, clock, events=None, recorder=None, favorites=None, sleeps=None):
    async def nosleep(s):
        if sleeps is not None:
            sleeps.append(s)

    return CaptureService(events or (lambda: _aiter([])), FakeMa(), PlayTracker(conn, clock), FakeSource(),
                          recorder or FakeRecorder(), favorites or FakeFavorites(), clock, sleep=nosleep)


async def _aiter(items):
    for i in items:
        yield i


async def test_played_events_set_source_and_record_closed_plays(conn):
    clock, rec = Clock(), FakeRecorder()
    svc = service(conn, clock, recorder=rec)
    await svc.handle(played("tidal--T://track/1"))
    assert conn.execute("SELECT source_ma_playlist_id FROM open_play").fetchone()[0] == "109"
    clock.t += timedelta(seconds=60)
    await svc.handle(played("tidal--T://track/2"))
    assert [c.play.uri for c in rec.plays] == ["tidal--T://track/1"]
    assert rec.plays[0].play.source_ma_playlist_id == "109"


async def test_favorite_and_sync_events_are_routed(conn):
    fav = FakeFavorites()
    svc = service(conn, Clock(), favorites=fav)
    await svc.handle({"event": "media_item_updated", "data": {"uri": "library://track/1", "media_type": "track",
                                                               "favorite": True}})
    await svc.handle({"event": "tasks_updated", "data": [{"id": "t1", "status": "running"}]})
    await svc.handle({"event": "music_sync_completed", "data": {}})
    assert fav.updates == ["library://track/1"] and fav.sync == [True, "done"]


async def test_tick_sweeps_releases_and_retries_on_schedule(conn):
    clock, rec, fav = Clock(), FakeRecorder(), FakeFavorites()
    svc = service(conn, clock, recorder=rec, favorites=fav)
    await svc.handle(played("tidal--T://track/1", secs=50))
    fav.due = [("library://track/5", T0)]
    clock.t += timedelta(minutes=11)
    await svc.tick()
    assert [c.ended_by for c in rec.plays] == ["idle"] and rec.favorites == ["library://track/5"]
    assert rec.retries == 1
    await svc.tick()
    assert rec.retries == 1          # next retry only after 15 min
    clock.t += timedelta(minutes=15)
    await svc.tick()
    assert rec.retries == 2


async def test_handler_error_does_not_stop_the_stream(conn):
    class Boom(FakeRecorder):
        async def record_play(self, c):
            raise RuntimeError("boom")

    events = [played("tidal--T://track/1"), played("tidal--T://track/2"), played("tidal--T://track/3")]
    svc = service(conn, Clock(), events=lambda: _aiter(events), recorder=Boom())
    await svc._stream_loop(max_rounds=1)
    assert conn.execute("SELECT ma_item_uri FROM open_play").fetchone()[0] == "tidal--T://track/3"


async def test_service_reconnects_after_stream_error(conn):
    calls, sleeps, fav = [], [], FakeFavorites()

    def events():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("connection refused")
        return _aiter([])

    svc = service(conn, Clock(), events=events, favorites=fav, sleeps=sleeps)
    await svc._stream_loop(max_rounds=3)
    assert len(calls) == 3 and sleeps[:2] == [1, 2]
    assert fav.loaded == ["library://track/1"]   # favorites baseline reloaded on connect


async def test_stream_state_is_reported_on_tick(conn):
    states, calls = [], []

    def events():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("refused")
        return _aiter([])

    svc = service(conn, Clock(), events=events)
    svc._on_state = states.append
    await svc._stream_loop(max_rounds=1)
    await svc.tick()
    svc._up = True
    await svc.tick()
    assert states == [False, True]
