from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.queue.results import apply_result, lose_job, mark_started, max_runtime, recover_running
from bridge.timeutil import iso

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def job(conn, jid=1, kind="play", target=240_000):
    conn.execute("INSERT INTO taste_event(id, source, ma_item_uri, provider_item_id, started_at, outcome, "
                 "policy_result) VALUES (?, 'ma_play', 'u', ?, 't', 'completed', 'eligible')", (jid, str(jid)))
    conn.execute("INSERT INTO feedback_job(id, taste_event_id, kind, spotify_id, target_ms, original_ts, status, "
                 "created_at) VALUES (?, ?, ?, 's', ?, 't', 'pending', 't')", (jid, jid, kind, target))
    mark_started(conn, jid, NOW)


def row(conn, jid=1):
    return conn.execute("SELECT * FROM feedback_job WHERE id = ?", (jid,)).fetchone()


def res(outcome, verified=False, reason=None, interrupt=None, progress=0, observed="s"):
    return {"outcome": outcome, "verified": verified, "reason": reason, "interrupt": interrupt,
            "progress_ms": progress, "observed_track": observed, "pause_confirmed": True}


def test_mark_started(conn):
    job(conn)
    r = row(conn)
    assert (r["status"], r["attempts"], r["started_at"]) == ("running", 1, iso(NOW))
    assert tuple(conn.execute("SELECT job_id, ts FROM job_start").fetchone()) == (1, iso(NOW))


def test_play_done_verified_unverified_and_short(conn):
    job(conn, 1)
    assert apply_result(conn, 1, res("done", verified=True), NOW, 3) == "verified"
    assert row(conn, 1)["observed_spotify_id"] == "s" and row(conn, 1)["finished_at"] == iso(NOW)
    job(conn, 2)
    assert apply_result(conn, 2, res("done", verified=False), NOW, 3) == "unverified"
    job(conn, 3, target=20_000)
    assert apply_result(conn, 3, res("done", verified=False), NOW, 3) == "done"
    job(conn, 4, target=20_000)
    assert apply_result(conn, 4, res("done", verified=True), NOW, 3) == "done"   # under 30 s is never verified


def test_save_done(conn):
    job(conn, kind="save", target=None)
    assert apply_result(conn, 1, res("done"), NOW, 3) == "done"


def test_failures_back_off_then_fail(conn):
    job(conn)
    assert apply_result(conn, 1, res("failed", reason="wrong_track"), NOW, 3) == "pending"
    r = row(conn)
    assert (r["next_attempt_at"], r["error"]) == (iso(NOW + timedelta(minutes=1)), "wrong_track")
    mark_started(conn, 1, NOW)
    assert apply_result(conn, 1, res("failed", reason="error"), NOW, 3) == "pending"
    assert row(conn)["next_attempt_at"] == iso(NOW + timedelta(minutes=5))
    mark_started(conn, 1, NOW)
    assert apply_result(conn, 1, res("failed", reason="error"), NOW, 3) == "failed"


def test_interrupted_by_pause_with_progress_is_verified(conn):
    job(conn, 1)
    assert apply_result(conn, 1, res("interrupted", verified=True, interrupt="pause", progress=45_000), NOW, 3) == \
        "verified"


def test_pause_returns_without_attempt(conn):
    job(conn, 1)
    assert apply_result(conn, 1, res("interrupted", interrupt="pause", progress=10_000), NOW, 3) == "pending"
    r = row(conn)
    assert (r["attempts"], r["next_attempt_at"]) == (0, None)


def test_other_device_waits_and_does_not_count_a_start(conn):
    job(conn, 1)
    assert apply_result(conn, 1, res("interrupted", interrupt="elsewhere"), NOW, 3) == "pending"
    r = row(conn)
    assert (r["attempts"], r["next_attempt_at"]) == (0, iso(NOW + timedelta(minutes=5)))
    assert conn.execute("SELECT count(*) FROM job_start").fetchone()[0] == 0


def test_takeover_counts_as_failed_attempt(conn):
    job(conn, 1)
    assert apply_result(conn, 1, res("interrupted", interrupt="takeover", progress=10_000), NOW, 3) == "pending"
    assert row(conn)["attempts"] == 1 and row(conn)["error"] == "takeover"


def test_recovery_unknown_play_job_is_unverified(conn):
    job(conn, 1)
    job(conn, 2, kind="save", target=None)
    job(conn, 3)
    n = recover_running(conn, {1: None, 2: None, 3: {"status": "finished", "result": res("done", verified=True)}},
                        NOW, 3)
    assert n == 3
    assert [row(conn, i)["status"] for i in (1, 2, 3)] == ["unverified", "pending", "verified"]
    assert row(conn, 2)["attempts"] == 0      # a lost save is retried without using an attempt


def test_lose_job_and_max_runtime(conn):
    job(conn, 1, target=240_000)
    assert max_runtime(row(conn, 1)) == timedelta(minutes=19)
    assert lose_job(conn, 1, NOW, "timeout") == "unverified" and row(conn, 1)["error"] == "timeout"
    job(conn, 2, kind="save", target=None)
    assert max_runtime(row(conn, 2)) == timedelta(minutes=10)
