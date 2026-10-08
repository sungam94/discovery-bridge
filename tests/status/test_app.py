import re

import pytest
from fastapi.testclient import TestClient

from bridge.db import connect, migrate
from bridge.status.app import create_app

CSRF = re.compile(r'name="csrf" value="([^"]+)"')


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "b.sqlite"
    migrate(connect(db))
    triggered = []
    app = create_app(db, "pw", "s" * 32, trigger=lambda: triggered.append(1), warnings=["backup_copy_target is not set"],
                     tz="Europe/Berlin")
    return TestClient(app, base_url="https://testserver"), db, triggered


def login(c, password="pw"):
    token = CSRF.search(c.get("/login").text).group(1)
    return c.post("/login", data={"password": password, "csrf": token}, follow_redirects=False)


def csrf(c):
    return CSRF.search(c.get("/").text).group(1)


def test_requires_login(env):
    c, _, _ = env
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_and_overview_shows_warnings(env):
    c, _, _ = env
    assert login(c).status_code == 303
    r = c.get("/")
    assert r.status_code == 200 and "Ingestion" in r.text and "backup_copy_target is not set" in r.text


def test_wrong_password_and_lockout(env):
    c, _, _ = env
    for _ in range(5):
        assert login(c, "no").status_code == 401
    assert login(c).status_code == 429


def test_non_ascii_password_is_rejected_not_500(env):
    c, _, _ = env
    assert login(c, "pässwört").status_code == 401


def test_toggle_ingestion_requires_csrf_and_triggers(env):
    c, db, triggered = env
    login(c)
    assert c.post("/settings/ingestion_paused", data={"csrf": "bad"}).status_code == 403
    assert c.post("/settings/ingestion_paused", data={"csrf": csrf(c)}, follow_redirects=False).status_code == 303
    assert connect(db).execute("SELECT value FROM setting WHERE key='ingestion_paused'").fetchone()[0] == "true"
    assert triggered == [1]


def test_forward_override_invalidates_mapping_and_triggers(env):
    c, db, triggered = env
    conn = connect(db)
    conn.execute("INSERT INTO track_mapping(direction, spotify_id, status, resolved_at) "
                 "VALUES ('forward','X','matched','2026-10-04T00:00:00+00:00')")
    login(c)
    r = c.post("/overrides", data={"csrf": csrf(c), "spotify_id": "X", "ma_provider_uri": "tidal--test0001://track/1"},
               follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT * FROM manual_override").fetchone()
    assert (row["direction"], row["spotify_id"], row["ma_provider_uri"], row["blocked"]) == (
        "forward", "X", "tidal--test0001://track/1", 0)
    assert conn.execute("SELECT status FROM track_mapping").fetchone()[0] == "invalidated"
    assert triggered == [1]


def test_block_override(env):
    c, db, _ = env
    login(c)
    c.post("/overrides", data={"csrf": csrf(c), "spotify_id": "Y", "block": "1"})
    assert tuple(connect(db).execute("SELECT blocked, ma_provider_uri FROM manual_override").fetchone()) == (1, None)


def test_pin_requires_tidal_uri(env):
    c, _, _ = env
    login(c)
    assert c.post("/overrides", data={"csrf": csrf(c), "spotify_id": "Z", "ma_provider_uri": "x"}).status_code == 400


def test_overview_shows_global_state_and_recovery_rows(env):
    c, db, _ = env
    conn = connect(db)
    conn.execute("INSERT INTO setting(key, value) VALUES ('ingestion_state', 'auth_expired')")
    conn.execute("INSERT INTO ingestion_health(slot, ts, state, detail) VALUES (NULL, 't1', 'auth_expired', 'cookie')")
    conn.execute("INSERT INTO ingestion_health(slot, ts, state, detail) VALUES (NULL, 't2', 'ok', '')")
    login(c)
    text = c.get("/").text
    assert 'id="ingestion-state"' in text and "auth_expired" in text.split('id="ingestion-state"')[1][:200]
    assert "t2" in text  # the recovery (ok) row is visible, so the owner sees the problem ended


def test_overview_shows_taste_events_and_weekly_metric(env):
    from bridge.timeutil import iso, utcnow
    c, db, _ = env
    conn = connect(db)
    for i, policy in enumerate(["feedback_paused", "feedback_paused", "player_unknown"]):
        conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, player_ids, started_at, outcome, "
                     "policy_result) VALUES ('ma_play', 'u', ?, 'zz-player', ?, 'completed', ?)",
                     (str(i), iso(utcnow()), policy))
    conn.execute("INSERT INTO playlist(slot, kind, name) VALUES ('discover_weekly', 'fixed', 'x')")
    for sid, matched in ((1, 30), (2, 0)):
        conn.execute("INSERT INTO playlist_snapshot(id, slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                     "VALUES (?, 'discover_weekly', 'dw', ?, 'o', ?)", (sid, f"2026-09-2{sid}T04:00:00+00:00", str(sid)))
        conn.execute("INSERT INTO weekly_metric VALUES (?, ?, NULL, ?, 15, 6, 3, NULL)",
                     (sid, f"2026-09-2{sid}T04:00:00+00:00", matched))
    login(c)
    text = c.get("/").text
    assert "Taste events today" in text and "feedback_paused" in text and "zz-player" in text
    assert "50 %" in text and "20 %" in text and "10 %" in text and "no coverage" in text


def test_reverse_override_pins_and_requeues_events(env):
    c, db, triggered = env
    conn = connect(db)
    uri = "tidal--test0001://track/9"
    conn.execute("INSERT INTO track_mapping(direction, ma_provider_uri, status, unmatched_reason, resolved_at) "
                 "VALUES ('reverse', ?, 'unmatched', 'no_match', '2026-10-05T00:00:00+00:00')", (uri,))
    conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider, provider_item_id, started_at, outcome, "
                 "policy_result) VALUES ('ma_play', ?, 'tidal--test0001', '9', 't', 'completed', 'unresolved')", (uri,))
    login(c)
    assert "Unresolved plays" in c.get("/").text and uri in c.get("/").text
    r = c.post("/overrides/reverse", data={"csrf": csrf(c), "ma_provider_uri": uri, "spotify_id": "sp9"},
               follow_redirects=False)
    assert r.status_code == 303
    o = conn.execute("SELECT direction, spotify_id, ma_provider_uri, blocked FROM manual_override").fetchone()
    assert tuple(o) == ("reverse", "sp9", uri, 0)
    assert conn.execute("SELECT status FROM track_mapping").fetchone()[0] == "invalidated"
    assert conn.execute("SELECT policy_result FROM taste_event").fetchone()[0] == "pending_resolve"


def test_reverse_pin_needs_a_spotify_id(env):
    c, _, _ = env
    login(c)
    r = c.post("/overrides/reverse", data={"csrf": csrf(c), "ma_provider_uri": "tidal--X://track/1"})
    assert r.status_code == 400


def test_feedback_switch_and_section(tmp_path):
    db = tmp_path / "b.sqlite"
    migrate(connect(db))
    woken = []
    app = create_app(db, "pw", "s" * 32, feedback_trigger=lambda: woken.append(1), tz="Europe/Berlin")
    c = TestClient(app, base_url="https://testserver")
    connect(db).execute("INSERT INTO adapter_health(ts, state, detail) VALUES ('2026-10-06T10:00:00+00:00', 'paused', '')")
    login(c)
    text = c.get("/").text
    assert "Feedback to Spotify" in text and "0/50" in text
    r = c.post("/settings/feedback_paused", data={"csrf": csrf(c)}, follow_redirects=False)
    assert r.status_code == 303 and woken == [1]
    assert connect(db).execute("SELECT value FROM setting WHERE key='feedback_paused'").fetchone()[0] == "false"


def test_feedback_switch_requires_csrf(env):
    c, _, _ = env
    login(c)
    assert c.post("/settings/feedback_paused", data={"csrf": "bad"}).status_code == 403


def test_status_page_shows_times_in_the_given_timezone(tmp_path):
    """"Today" on the status page is the local day of the given timezone, not the UTC day."""
    from datetime import datetime, timezone
    db = tmp_path / "b.sqlite"
    conn = connect(db)
    migrate(conn)
    for policy, started in (("berlin_today", "2026-10-04T22:30:00+00:00"),      # 00:30 on the 5th in Berlin
                            ("berlin_yesterday", "2026-10-04T21:30:00+00:00")):  # 23:30 on the 4th in Berlin
        conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, player_ids, started_at, outcome, "
                     "policy_result) VALUES ('ma_play', 'u', ?, 'p', ?, 'completed', ?)", (policy, started, policy))
    app = create_app(db, "pw", "s" * 32, now=lambda: datetime(2026, 10, 5, 8, tzinfo=timezone.utc),
                     tz="Europe/Berlin")
    c = TestClient(app, base_url="https://testserver")
    login(c)
    text = c.get("/").text
    assert "berlin_today" in text and "berlin_yesterday" not in text
