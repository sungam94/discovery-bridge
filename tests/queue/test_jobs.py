from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.queue.jobs import create_jobs
from bridge.timeutil import iso

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
AGE = 48 * 3600


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def event(conn, eid, outcome, policy="eligible", spotify="s1", listened=130_000, duration=240_000,
          source="sp_dw", started=None):
    started = started or iso(NOW - timedelta(hours=2))
    conn.execute("INSERT INTO taste_event(id, source, ma_item_uri, provider_item_id, started_at, listened_ms, "
                 "duration_ms, outcome, spotify_id, source_spotify_playlist_id, policy_result) "
                 "VALUES (?, ?, 'u', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (eid, "ma_favorite" if outcome == "favorite" else "ma_play", str(eid), started, listened, duration,
                  outcome, spotify, source, policy))


def jobs(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT taste_event_id, kind, spotify_id, target_ms, source_spotify_playlist_id, status "
        "FROM feedback_job ORDER BY taste_event_id")]


def policy(conn, eid):
    return conn.execute("SELECT policy_result FROM taste_event WHERE id = ?", (eid,)).fetchone()[0]


def test_completed_listened_and_favorite_become_jobs(conn):
    event(conn, 1, "completed")
    event(conn, 2, "listened", spotify="s2")
    event(conn, 3, "favorite", spotify="s3", listened=None, duration=None, source=None)
    assert create_jobs(conn, NOW, AGE) == 3
    assert jobs(conn) == [(1, "play", "s1", 240_000, "sp_dw", "pending"),
                          (2, "play", "s2", 130_000, "sp_dw", "pending"),
                          (3, "save", "s3", None, None, "pending")]


def test_only_eligible_recent_events_and_only_once(conn):
    event(conn, 1, "completed", policy="feedback_paused")
    event(conn, 2, "completed")
    event(conn, 3, "completed", started=iso(NOW - timedelta(hours=49)))      # older than max_job_age
    assert create_jobs(conn, NOW, AGE) == 1
    assert create_jobs(conn, NOW, AGE) == 0
    assert [j[0] for j in jobs(conn)] == [2]


def test_duplicate_save_is_marked_and_not_rescanned(conn):
    event(conn, 1, "favorite", spotify="s9")
    create_jobs(conn, NOW, AGE)
    conn.execute("UPDATE feedback_job SET status = 'done'")
    event(conn, 2, "favorite", spotify="s9")
    assert create_jobs(conn, NOW, AGE) == 0
    assert policy(conn, 2) == "duplicate_save"
    conn.execute("UPDATE feedback_job SET status = 'failed'")
    event(conn, 3, "favorite", spotify="s9")
    assert create_jobs(conn, NOW, AGE) == 1          # an earlier failed save does not block a new one


def test_play_without_target_is_marked(conn):
    event(conn, 1, "completed", duration=None)
    assert create_jobs(conn, NOW, AGE) == 0 and policy(conn, 1) == "no_target"


def test_events_without_spotify_id_are_skipped(conn):
    event(conn, 1, "completed", spotify=None)
    assert create_jobs(conn, NOW, AGE) == 0
