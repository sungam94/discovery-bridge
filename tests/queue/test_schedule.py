from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bridge.db import connect, migrate
from bridge.queue.schedule import backoff_after, daypart, expire_jobs, next_job, started_today, bucket
from bridge.timeutil import iso

TZ = "Europe/Berlin"
B = ZoneInfo(TZ)


def local(*a):
    return datetime(*a, tzinfo=B).astimezone(timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def job(conn, jid, kind="play", outcome="completed", original=None, status="pending", next_at=None):
    original = original or local(2026, 10, 6, 13, 0)
    conn.execute("INSERT INTO taste_event(id, source, ma_item_uri, provider_item_id, started_at, outcome, "
                 "policy_result) VALUES (?, 'ma_play', 'u', ?, ?, ?, 'eligible')", (jid, str(jid), iso(original), outcome))
    conn.execute("INSERT INTO feedback_job(id, taste_event_id, kind, spotify_id, original_ts, status, next_attempt_at, "
                 "created_at) VALUES (?, ?, ?, 's', ?, ?, ?, 't')",
                 (jid, jid, kind, iso(original), status, iso(next_at) if next_at else None))


def start(conn, jid, at):
    conn.execute("INSERT INTO job_start(job_id, ts) VALUES (?, ?)", (jid, iso(at)))


def test_daypart():
    assert [daypart(local(2026, 10, 6, h, 30), TZ) for h in (0, 5, 6, 11, 12, 17, 18, 23)] == [0, 0, 1, 1, 2, 2, 3, 3]


def test_dayparts_across_dst():
    # 2026-10-25: clocks go back 03:00 -> 02:00, so 02:30 happens twice; both are night.
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)   # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)  # 02:30 CET
    assert daypart(first, TZ) == daypart(second, TZ) == 0
    assert bucket(first, TZ) == bucket(second, TZ) == 2
    # 2026-03-29: clocks go forward 02:00 -> 03:00
    assert daypart(datetime(2026, 3, 29, 4, 30, tzinfo=timezone.utc), TZ) == 1  # 06:30 CEST


def test_backoff():
    assert [backoff_after(n) for n in (1, 2, 3, 4)] == [timedelta(minutes=1), timedelta(minutes=5),
                                                         timedelta(minutes=10), timedelta(minutes=20)]


def test_daily_cap_counts_starts_on_the_local_day(conn):
    now = local(2026, 10, 7, 14, 0)
    job(conn, 1, status="failed")
    start(conn, 1, local(2026, 10, 7, 0, 30))
    start(conn, 1, local(2026, 10, 7, 0, 31))                                  # a retry counts as a second start
    start(conn, 1, local(2026, 10, 6, 23, 30))                                 # yesterday, local time
    job(conn, 10, original=local(2026, 10, 6, 14, 10))                         # yesterday, the same hour
    assert started_today(conn, now, TZ) == 2
    assert next_job(conn, now, TZ, 2) is None
    assert next_job(conn, now, TZ, 30)["id"] == 10


def test_tracks_spotify_already_got_a_replay_of_wait_behind_new_ones(conn):
    now = local(2026, 10, 7, 14, 40)
    job(conn, 1, original=local(2026, 10, 6, 13, 0), status="verified")                 # T1 replayed yesterday
    job(conn, 2, original=local(2026, 10, 7, 14, 5))                                     # T1 heard again today
    job(conn, 3, outcome="listened", original=local(2026, 10, 7, 14, 30))                # T3, new, only listened
    conn.execute("UPDATE feedback_job SET spotify_id = 'T1' WHERE id IN (1, 2)")
    conn.execute("UPDATE feedback_job SET spotify_id = 'T3' WHERE id = 3")
    assert next_job(conn, now, TZ, 50)["id"] == 3          # a new track beats a repeat, even a completed one
    conn.execute("UPDATE feedback_job SET status = 'verified' WHERE id = 3")
    assert next_job(conn, now, TZ, 50)["id"] == 2          # the repeat still runs when nothing new is waiting


def test_bucket_is_the_local_hour_or_a_wider_slot():
    assert bucket(local(2026, 10, 7, 14, 59), TZ) == 14 and bucket(local(2026, 10, 7, 0, 0), TZ) == 0
    assert bucket(local(2026, 10, 7, 14, 59), TZ, hours=2) == 7


def test_next_job_prefers_saves_then_new_completed_oldest_in_the_current_hour(conn):
    now = local(2026, 10, 7, 14, 30)
    job(conn, 1, outcome="listened", original=local(2026, 10, 7, 14, 5))
    job(conn, 2, outcome="completed", original=local(2026, 10, 7, 14, 10))
    job(conn, 3, outcome="completed", original=local(2026, 10, 3, 14, 50))    # four days ago, same hour: oldest
    assert next_job(conn, now, TZ, 50)["id"] == 3
    job(conn, 4, kind="save", outcome="favorite", original=local(2026, 10, 7, 9, 0))
    assert next_job(conn, now, TZ, 50)["id"] == 4


def test_a_play_waits_for_its_hour_on_any_later_day_until_it_is_too_old(conn):
    job(conn, 1, original=local(2026, 10, 7, 8, 15))
    assert next_job(conn, local(2026, 10, 7, 14, 0), TZ, 50) is None                 # other hour
    assert next_job(conn, local(2026, 10, 12, 8, 40), TZ, 50)["id"] == 1             # five days later, 8 o'clock
    assert next_job(conn, local(2026, 10, 22, 8, 40), TZ, 50) is None                # older than 14 days


def test_next_job_respects_backoff(conn):
    now = local(2026, 10, 7, 14, 0)
    job(conn, 3, original=local(2026, 10, 7, 14, 0), next_at=now + timedelta(minutes=1))
    assert next_job(conn, now, TZ, 50) is None
    assert next_job(conn, now + timedelta(minutes=1), TZ, 50)["id"] == 3


def test_the_daily_limit_is_spread_over_the_day(conn):
    for i in range(10):
        job(conn, 100 + i, status="verified", original=local(2026, 10, 6, 1, i))
        start(conn, 100 + i, local(2026, 10, 7, 0, 30 + i))
    job(conn, 1, original=local(2026, 10, 6, 3, 0))
    job(conn, 2, original=local(2026, 10, 6, 20, 0))
    assert next_job(conn, local(2026, 10, 7, 3, 5), TZ, 48) is None       # 10 starts by 4 o'clock: 48 * 4/24 = 8
    assert next_job(conn, local(2026, 10, 7, 20, 5), TZ, 48)["id"] == 2   # evening budget is still there


def test_expiry(conn):
    now = local(2026, 10, 22, 12, 0)
    job(conn, 1, original=now - timedelta(days=14, minutes=1))
    job(conn, 2, original=now - timedelta(days=13))
    job(conn, 3, kind="save", outcome="favorite", original=now - timedelta(hours=49))
    job(conn, 4, kind="save", outcome="favorite", original=now - timedelta(hours=47))
    assert expire_jobs(conn, now, TZ, 48 * 3600) == 2
    got = dict(conn.execute("SELECT id, expire_reason FROM feedback_job WHERE status = 'expired'").fetchall())
    assert got == {1: "max_age", 3: "max_age"}
