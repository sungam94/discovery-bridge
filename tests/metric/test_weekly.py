import pytest

from bridge.db import connect, migrate
from bridge.metric.weekly import compute_weekly
from tests.capture.helpers import seed_snapshot

U = "tidal--T://track/{}"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def ev(conn, spotify_id, outcome, snapshot_id=None, source="ma_play", started_at="2026-09-29T10:00:00+00:00",
       n=[0]):
    n[0] += 1
    conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, started_at, outcome, spotify_id, "
                 "source_snapshot_id, policy_result) VALUES (?, 'u', ?, ?, ?, ?, ?, 'feedback_paused')",
                 (source, str(n[0]), started_at, outcome, spotify_id, snapshot_id))


def rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT snapshot_id, window_start, window_end, matched, played, completed, favorited, feedback_days "
        "FROM weekly_metric ORDER BY snapshot_id")]


def test_windows_follow_set_changes_not_reorders(conn):
    a = [("a1", U.format(1)), ("a2", U.format(2)), ("a3", U.format(3)), ("a4", None)]
    s1 = seed_snapshot(conn, "discover_weekly", "109", a, set_hash="A", fetched_at="2026-09-28T04:00:00+00:00")
    s2 = seed_snapshot(conn, "discover_weekly", "109", list(reversed(a)), set_hash="A",
                       fetched_at="2026-09-29T04:00:00+00:00")
    s3 = seed_snapshot(conn, "discover_weekly", "109", [("b1", U.format(11))], set_hash="B",
                       fetched_at="2026-10-05T04:00:00+00:00")
    ev(conn, "a1", "skipped", s1)
    ev(conn, "a1", "completed", s2)          # best outcome of a1 is completed
    ev(conn, "a2", "listened", s1)
    ev(conn, "a3", "skipped", s2)
    ev(conn, "a2", "completed", None)        # no source snapshot: does not count
    ev(conn, "a3", "favorite", source="ma_favorite", started_at="2026-10-01T10:00:00+00:00")
    ev(conn, "b1", "favorite", source="ma_favorite", started_at="2026-10-01T10:00:00+00:00")  # not in set A
    assert compute_weekly(conn) == 2
    assert rows(conn) == [
        (s1, "2026-09-28T04:00:00+00:00", "2026-10-05T04:00:00+00:00", 3, 3, 2, 1, None),
        (s3, "2026-10-05T04:00:00+00:00", None, 1, 0, 0, 0, None),
    ]


def test_zero_coverage_window(conn):
    s1 = seed_snapshot(conn, "discover_weekly", "109", [("a1", None)], set_hash="A")
    compute_weekly(conn)
    assert rows(conn)[0][3:5] == (0, 0)


def test_recompute_is_idempotent(conn):
    seed_snapshot(conn, "discover_weekly", "109", [("a1", U.format(1))], set_hash="A")
    compute_weekly(conn)
    compute_weekly(conn)
    assert len(rows(conn)) == 1


def test_guest_players_do_not_count(conn):
    s1 = seed_snapshot(conn, "discover_weekly", "109", [("a1", U.format(1)), ("a2", U.format(2))], set_hash="A")
    ev(conn, "a1", "completed", s1)
    conn.execute("UPDATE taste_event SET policy_result = 'player_not_allowed'")
    ev(conn, "a2", "skipped", s1)
    compute_weekly(conn)
    assert rows(conn)[0][4:6] == (1, 0)  # only a2 (skipped) counts as played
