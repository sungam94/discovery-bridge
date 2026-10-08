from datetime import datetime, timedelta, timezone

import pytest

from bridge.capture.favorites import BURST_MAX, HOLD, SYNC_GRACE, FavoriteTracker
from bridge.db import connect, migrate

T0 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


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


def upd(uri="library://track/1", fav=True, media_type="track"):
    return {"uri": uri, "media_type": media_type, "favorite": fav}


def tracker(conn, clock, favs=()):
    f = FavoriteTracker(conn, clock)
    f.load(favs)
    return f


def test_new_favorite_is_held_then_released(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_item_updated(upd())
    assert f.release_due() == []
    clock.t += HOLD
    assert f.release_due() == [("library://track/1", T0)]
    assert f.release_due() == []


def test_unfavorite_within_hold_drops_it(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_item_updated(upd())
    clock.t += timedelta(minutes=2)
    f.on_item_updated(upd(fav=False))
    clock.t += HOLD
    assert f.release_due() == []


def test_already_favorite_updates_are_not_new(conn):
    clock = Clock()
    f = tracker(conn, clock, favs=["library://track/1"])
    f.on_item_updated(upd())  # metadata refresh of an existing favorite
    clock.t += HOLD
    assert f.release_due() == []


def test_updates_before_load_set_the_baseline_only(conn):
    clock = Clock()
    f = FavoriteTracker(conn, clock)
    f.on_item_updated(upd())
    clock.t += HOLD
    assert f.release_due() == []


def test_non_tracks_are_ignored(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_item_updated(upd(uri="library://album/1", media_type="album"))
    clock.t += HOLD
    assert f.release_due() == []


def test_sync_completed_event_starts_grace(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_sync_completed()
    f.on_item_updated(upd())
    clock.t += HOLD
    assert f.release_due() == []


def test_burst_drops_held_favorites(conn):
    clock = Clock()
    f = tracker(conn, clock)
    for i in range(BURST_MAX + 1):
        f.on_item_updated(upd(f"library://track/{i}"))
        clock.t += timedelta(seconds=2)
    f.on_item_updated(upd("library://track/99"))  # still inside the burst
    clock.t += HOLD + timedelta(minutes=1)
    assert f.release_due() == []


def test_ten_favorites_are_not_a_burst(conn):
    clock = Clock()
    f = tracker(conn, clock)
    for i in range(BURST_MAX):
        f.on_item_updated(upd(f"library://track/{i}"))
    clock.t += HOLD
    assert len(f.release_due()) == BURST_MAX


def test_held_favorites_survive_restart(conn):
    clock = Clock()
    tracker(conn, clock).on_item_updated(upd())
    clock.t += HOLD
    assert tracker(conn, clock).release_due() == [("library://track/1", T0)]


def sync_task(status):
    return [{"id": "t1", "status": status, "metadata": {"task_domain": "music_sync"}},
            {"id": "t2", "status": "running", "metadata": {"task_domain": "metadata"}}]


def test_music_sync_tasks_open_the_window(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_tasks_updated(sync_task("running"))
    f.on_item_updated(upd("library://track/1"))
    f.on_tasks_updated(sync_task("success"))
    clock.t += timedelta(minutes=5)
    f.on_item_updated(upd("library://track/2"))   # inside the grace period
    clock.t += SYNC_GRACE
    f.on_item_updated(upd("library://track/3"))
    clock.t += HOLD
    assert [u for u, _ in f.release_due()] == ["library://track/3"]


def test_other_tasks_do_not_open_the_window(conn):
    clock = Clock()
    f = tracker(conn, clock)
    f.on_tasks_updated([{"id": "t2", "status": "running", "metadata": {"task_domain": "metadata"}}])
    f.on_item_updated(upd())
    clock.t += HOLD
    assert len(f.release_due()) == 1
