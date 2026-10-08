from datetime import datetime, timedelta, timezone

import pytest

from bridge.capture.plays import IDLE_END, PlayTracker
from bridge.db import connect, migrate

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def ev(uri="tidal--T://track/1", secs=0, fully=False, player="p1", duration=240, media_type="track"):
    return {"uri": uri, "media_type": media_type, "duration": duration, "seconds_played": secs,
            "fully_played": fully, "is_playing": True, "player_id": player, "userid": "u1"}


def test_first_event_starts_a_play(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    closed, started = tr.on_played(ev(secs=30))
    assert closed == [] and started.uri == "tidal--T://track/1"
    assert started.started_at == T0 - timedelta(seconds=30) and started.duration_ms == 240_000


def test_next_item_closes_previous_play(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=0))
    clock.t += timedelta(seconds=60)
    tr.on_played(ev(secs=60))
    clock.t += timedelta(seconds=5)
    closed, started = tr.on_played(ev(uri="tidal--T://track/2", secs=0))
    assert [(c.play.uri, c.ended_by, c.listened_ms) for c in closed] == [("tidal--T://track/1", "next", 60_000)]
    assert started.uri == "tidal--T://track/2"


def test_progress_events_continue_the_play(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    _, first = tr.on_played(ev(secs=0))
    clock.t += timedelta(seconds=30)
    closed, started = tr.on_played(ev(secs=30))
    assert closed == [] and started is None
    assert conn.execute("SELECT seconds_played FROM open_play").fetchone()[0] == 30


def test_repeat_one_restart_closes_and_starts_again(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=0))
    clock.t += timedelta(seconds=240)
    tr.on_played(ev(secs=240, fully=True))
    clock.t += timedelta(seconds=2)
    closed, started = tr.on_played(ev(secs=1))
    assert [(c.ended_by, c.play.fully_played) for c in closed] == [("restart", True)]
    assert started is not None and started.started_at == clock.t - timedelta(seconds=1)


def test_fully_played_is_sticky(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=239, fully=True))
    tr.on_played(ev(secs=240, fully=False))
    closed, _ = tr.on_played(ev(uri="tidal--T://track/2"))
    assert closed[0].play.fully_played is True


def test_players_are_independent(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(player="p1"))
    closed, started = tr.on_played(ev(uri="tidal--T://track/9", player="p2"))
    assert closed == [] and started.player_id == "p2"


def test_sweep_closes_idle_plays(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=50))
    clock.t += IDLE_END - timedelta(seconds=1)
    assert tr.sweep() == []
    clock.t += timedelta(seconds=2)
    closed = tr.sweep()
    assert [(c.ended_by, c.listened_ms) for c in closed] == [("idle", 50_000)]
    assert conn.execute("SELECT count(*) FROM open_play").fetchone()[0] == 0


def test_set_source_is_kept_on_the_open_play(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev())
    tr.set_source("p1", "tidal--T://track/1", "107", False, "q1")
    closed, _ = tr.on_played(ev(uri="tidal--T://track/2"))
    assert (closed[0].play.source_ma_playlist_id, closed[0].play.queue_id) == ("107", "q1")


def test_set_source_for_an_old_item_is_ignored(conn):
    tr = PlayTracker(conn, Clock())
    tr.on_played(ev(uri="tidal--T://track/2"))
    tr.set_source("p1", "tidal--T://track/1", "107", False, "q1")  # answer arrived after the track changed
    assert conn.execute("SELECT source_ma_playlist_id FROM open_play").fetchone()[0] is None


def test_restart_continues_open_play(conn):
    clock = Clock()
    PlayTracker(conn, clock).on_played(ev(secs=30))
    clock.t += timedelta(seconds=30)
    tr = PlayTracker(conn, clock)  # bridge restarted
    closed, started = tr.on_played(ev(secs=60))
    assert closed == [] and started is None


def test_restart_after_gap_closes_with_stored_values(conn):
    clock = Clock()
    PlayTracker(conn, clock).on_played(ev(secs=90))
    clock.t += timedelta(minutes=3)
    tr = PlayTracker(conn, clock)
    closed, started = tr.on_played(ev(uri="tidal--T://track/2", secs=10))
    assert [(c.play.uri, c.listened_ms, c.ended_by) for c in closed] == [("tidal--T://track/1", 90_000, "next")]
    assert started.uri == "tidal--T://track/2"


def test_missing_duration_is_none(conn):
    _, started = PlayTracker(conn, Clock()).on_played(ev(duration=None))
    assert started.duration_ms is None


def test_recorded_sequence_from_spike(conn, fixture):
    """Spike check 3: play 30 s, pause, then the next track (plan-inputs, Capture)."""
    tr = PlayTracker(conn, Clock())
    out = [tr.on_played(e["data"]) for e in fixture("check03_event_media_item_played")]
    assert out[1] == ([], None)  # the pause event continues the play
    closed = [c for cl, _ in out for c in cl]
    assert [(c.play.uri, c.listened_ms, c.ended_by) for c in closed] == [
        ("tidal--test0001://track/900026000", 30_000, "next")]


def test_restart_is_seen_at_the_first_progress_report(conn):
    """MA reports progress about every 30 s, so a restarted item first shows up at ~30 s, not near 0."""
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=356, fully=True))
    clock.t += timedelta(seconds=31)
    closed, started = tr.on_played(ev(secs=31))
    assert [(c.ended_by, c.listened_ms) for c in closed] == [("restart", 356_000)]
    assert started is not None


def test_small_seek_back_is_not_a_restart(conn):
    clock = Clock()
    tr = PlayTracker(conn, clock)
    tr.on_played(ev(secs=200))
    closed, started = tr.on_played(ev(secs=150))
    assert closed == [] and started is None
