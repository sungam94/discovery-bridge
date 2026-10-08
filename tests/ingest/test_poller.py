from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bridge.db import connect, migrate
from bridge.ingest.poller import Poller
from bridge.repo import set_setting
from bridge.spotify.client import AuthExpired, RateLimited

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
CFG = SimpleNamespace(discover_weekly_id="dw", release_radar_id="rr")
HUB = {"spotify:section:0JQ5DACFo5h0jxzOyHOsIo": [("m1", "Daily Mix 1"), ("m2", "Daily Mix 2")],
       "spotify:section:0JQ5DACFo5h0jxzOyHOsIe": [("dl", "daylist")]}


class FakeSpotify:
    def __init__(self, hub=None, fail=None):
        self.hub, self.fail = (HUB if hub is None else hub), list(fail or [])

    async def made_for_you(self):
        if self.fail:
            raise self.fail.pop(0)
        return self.hub


class Rec:
    def __init__(self, result="snapshot", conn=None, empty_slot=None, problems=()):
        self.calls, self.result, self.conn, self.empty_slot = [], result, conn, empty_slot
        self.last_problems = list(problems)

    async def ingest_slot(self, slot, spid):
        self.calls.append((slot, spid))
        if slot == self.empty_slot:
            self.conn.execute("UPDATE playlist SET empty_count = empty_count + 1 WHERE slot = ?", (slot,))
            return "empty"
        return self.result

    async def check(self, slot):
        return "published"


class FakeMa:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


async def nosleep(_):
    return None


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def poller(conn, spotify, rec, ma=None, now=NOW):
    return Poller(conn, spotify, rec, rec, ma or FakeMa(), CFG, lambda: now, sleep=nosleep)


async def test_run_once_covers_all_slots_and_closes_ma(conn):
    rec, ma = Rec(), FakeMa()
    out = await poller(conn, FakeSpotify(), rec, ma).run_once()
    assert rec.calls == [("discover_weekly", "dw"), ("release_radar", "rr"), ("daily_mix_1", "m1"),
                         ("daily_mix_2", "m2"), ("daylist", "dl")]
    assert out["daily_mix_1"] == "snapshot/published" and ma.closed == 1


async def test_paused(conn):
    set_setting(conn, "ingestion_paused", "true")
    assert await poller(conn, FakeSpotify(), Rec()).run_once() == {"*": "paused"}


async def test_auth_expired_records_health(conn):
    out = await poller(conn, FakeSpotify(fail=[AuthExpired("x")]), Rec()).run_once()
    assert out == {"*": "auth_expired"}
    assert conn.execute("SELECT state FROM ingestion_health").fetchone()["state"] == "auth_expired"


async def test_hub_rate_limit_waits_and_retries_once(conn):
    rec = Rec()
    await poller(conn, FakeSpotify(fail=[RateLimited(5)]), rec).run_once()
    assert ("daily_mix_1", "m1") in rec.calls


async def test_missing_daily_mix_section_does_not_age_slots(conn):
    await poller(conn, FakeSpotify(), Rec(), now=NOW).run_once()
    later = NOW + timedelta(days=9)
    rec = Rec()
    await poller(conn, FakeSpotify(hub={"spotify:section:0JQ5DACFo5h0jxzOyHOsIe": [("dl", "daylist")]}), rec,
                 now=later).run_once()
    row = conn.execute("SELECT spotify_id, stale FROM playlist WHERE slot='daily_mix_1'").fetchone()
    assert (row["spotify_id"], row["stale"]) == ("m1", 0)
    assert ("daily_mix_1", "m1") in rec.calls  # still ingested with its last known ID


async def test_three_empty_fetches_raise_error_state(conn):
    for _ in range(3):
        await poller(conn, FakeSpotify(), Rec(conn=conn, empty_slot="daylist")).run_once()
    row = conn.execute("SELECT last_state, last_error FROM playlist WHERE slot='daylist'").fetchone()
    assert row["last_state"] == "error" and "empty" in row["last_error"]


async def test_health_rows_only_on_state_change(conn):
    for _ in range(3):
        await poller(conn, FakeSpotify(), Rec()).run_once()
    n = conn.execute("SELECT count(*) FROM ingestion_health WHERE slot='discover_weekly'").fetchone()[0]
    assert n == 1


async def test_ingest_problems_set_error_state(conn):
    await poller(conn, FakeSpotify(), Rec(problems=["2 resolutions pending (ma_error)"])).run_once()
    row = conn.execute("SELECT last_state, last_error FROM playlist WHERE slot='discover_weekly'").fetchone()
    assert row["last_state"] == "error" and "ma_error" in row["last_error"]


FOR_YOU, ARTISTS = "spotify:section:0JQ5DACFo5h0jxzOyHOsIe", "spotify:section:0JQ5DACFo5h0jxzOyHOsIa"
HUB_CFG = SimpleNamespace(discover_weekly_id="dw", release_radar_id="rr", hub_sections=(FOR_YOU, ARTISTS),
                          language="de")


def hub_poller(conn, hub, rec, now=NOW):
    return Poller(conn, FakeSpotify(hub=hub), rec, rec, FakeMa(), HUB_CFG, lambda: now, sleep=nosleep)


async def test_hub_section_playlists_become_slots(conn):
    hub = {**HUB, FOR_YOU: [("dl", "daylist"), ("rep", "On Repeat")],
           ARTISTS: [("a1", "Hazy Fern Mix"), ("dw", "dup of a fixed playlist")],
           "spotify:section:0JQ5DACFo5h0jxzOyHOsI9": [("g1", "Metal Mix")]}  # section not configured
    rec = Rec()
    await hub_poller(conn, hub, rec).run_once()
    assert rec.calls[-2:] == [("hub:rep", "rep"), ("hub:a1", "a1")]
    rows = {r["slot"]: r for r in conn.execute("SELECT * FROM playlist WHERE kind = 'hub'")}
    assert set(rows) == {"hub:rep", "hub:a1"}
    assert rows["hub:a1"]["name"] == "Spotify · Hazy Fern Mix" and rows["hub:a1"]["section"] == ARTISTS
    assert (rows["hub:rep"]["hub_position"], rows["hub:a1"]["hub_position"]) == (1, 0)


async def test_hub_playlist_keeps_its_first_name(conn):
    await hub_poller(conn, {**HUB, ARTISTS: [("a1", "Hazy Fern Mix")]}, Rec()).run_once()
    await hub_poller(conn, {**HUB, ARTISTS: [("a1", "Hazy-Fern-Mix")]}, Rec()).run_once()
    assert conn.execute("SELECT name FROM playlist WHERE slot='hub:a1'").fetchone()["name"] == "Spotify · Hazy Fern Mix"


async def test_hub_playlist_gone_for_seven_days_turns_stale(conn):
    await hub_poller(conn, {**HUB, ARTISTS: [("a1", "Hazy Fern Mix")]}, Rec()).run_once()
    rec = Rec()
    await hub_poller(conn, HUB, rec, now=NOW + timedelta(days=3)).run_once()
    assert ("hub:a1", "a1") in rec.calls  # briefly missing: still refreshed
    rec = Rec()
    await hub_poller(conn, HUB, rec, now=NOW + timedelta(days=8)).run_once()
    assert ("hub:a1", "a1") not in rec.calls
    assert conn.execute("SELECT stale FROM playlist WHERE slot='hub:a1'").fetchone()["stale"] == 1
    await hub_poller(conn, {**HUB, ARTISTS: [("a1", "Hazy Fern Mix")]}, Rec(), now=NOW + timedelta(days=9)).run_once()
    assert conn.execute("SELECT stale FROM playlist WHERE slot='hub:a1'").fetchone()["stale"] == 0


async def test_poll_writes_layout_file(conn, tmp_path):
    cfg = SimpleNamespace(**vars(HUB_CFG), ma_layout_dir=tmp_path / "layout")
    cfg.ma_layout_dir.mkdir()
    await Poller(conn, FakeSpotify(), Rec(), Rec(), FakeMa(), cfg, lambda: NOW, sleep=nosleep).run_once()
    assert (tmp_path / "layout" / "layout.json").exists()


async def test_poll_layout_reads_the_sound_database(conn, tmp_path, monkeypatch):
    import bridge.ingest.poller as poller_module
    seen = []
    monkeypatch.setattr(poller_module, "write_layout", lambda *a, **kw: seen.append(kw.get("sound_path")))
    cfg = SimpleNamespace(**vars(HUB_CFG), ma_layout_dir=tmp_path / "layout", sound_db_path=tmp_path / "s.sqlite")
    await Poller(conn, FakeSpotify(), Rec(), Rec(), FakeMa(), cfg, lambda: NOW, sleep=nosleep).run_once()
    assert seen and set(seen) == {tmp_path / "s.sqlite"}


async def test_layout_is_written_after_each_slot(conn, tmp_path):
    cfg = SimpleNamespace(**vars(HUB_CFG), ma_layout_dir=tmp_path / "layout")
    cfg.ma_layout_dir.mkdir()
    seen = []

    class Watch(Rec):
        async def ingest_slot(self, slot, spid):
            seen.append((slot, (cfg.ma_layout_dir / "layout.json").exists()))
            return await super().ingest_slot(slot, spid)

    rec = Watch()
    await Poller(conn, FakeSpotify(), rec, rec, FakeMa(), cfg, lambda: NOW, sleep=nosleep).run_once()
    assert seen[0] == ("discover_weekly", False) and all(ok for _, ok in seen[1:])


async def test_poll_recomputes_weekly_metric(conn):
    from tests.capture.helpers import seed_snapshot
    seed_snapshot(conn, "discover_weekly", "109", [("a1", "tidal--T://track/1")], set_hash="A")
    await poller(conn, FakeSpotify(), Rec()).run_once()
    assert conn.execute("SELECT count(*) FROM weekly_metric").fetchone()[0] == 1


async def test_fixed_slots_use_the_configured_ids(conn):
    cfg = SimpleNamespace(discover_weekly_id="37i9dQZEVXcFakeDw00001", release_radar_id="37i9dQZEVXbFakeRr00001")
    rec = Rec()
    await Poller(conn, FakeSpotify(), rec, rec, FakeMa(), cfg, lambda: NOW, sleep=nosleep).run_once()
    assert rec.calls[:2] == [("discover_weekly", "37i9dQZEVXcFakeDw00001"),
                             ("release_radar", "37i9dQZEVXbFakeRr00001")]
    stored = dict(conn.execute("SELECT slot, spotify_id FROM playlist WHERE kind = 'fixed'").fetchall())
    assert stored == {"discover_weekly": "37i9dQZEVXcFakeDw00001", "release_radar": "37i9dQZEVXbFakeRr00001"}


NO_IDS = SimpleNamespace(discover_weekly_id=None, release_radar_id=None)
HUB_WITH_FIXED = {**HUB, "spotify:section:0JQ5DACFo5h0jxzOyHOsIf": [("37i9dQZEVXbFakeRr00001", "Release Radar"),
                                                                    ("37i9dQZEVXcFakeDw00001", "Discover Weekly")]}


async def test_fixed_ids_come_from_the_hub(conn):
    rec = Rec()
    await Poller(conn, FakeSpotify(hub=HUB_WITH_FIXED), rec, rec, FakeMa(), NO_IDS, lambda: NOW,
                 sleep=nosleep).run_once()
    assert rec.calls[:2] == [("discover_weekly", "37i9dQZEVXcFakeDw00001"),
                             ("release_radar", "37i9dQZEVXbFakeRr00001")]
    stored = dict(conn.execute("SELECT slot, spotify_id FROM playlist WHERE kind = 'fixed'").fetchall())
    assert stored == {"discover_weekly": "37i9dQZEVXcFakeDw00001", "release_radar": "37i9dQZEVXbFakeRr00001"}


async def test_config_override_beats_the_hub(conn):
    cfg = SimpleNamespace(discover_weekly_id="37i9dQZEVXcFakeDw00002", release_radar_id=None)
    rec = Rec()
    await Poller(conn, FakeSpotify(hub=HUB_WITH_FIXED), rec, rec, FakeMa(), cfg, lambda: NOW,
                 sleep=nosleep).run_once()
    assert rec.calls[:2] == [("discover_weekly", "37i9dQZEVXcFakeDw00002"),
                             ("release_radar", "37i9dQZEVXbFakeRr00001")]


async def test_stored_id_is_used_when_the_hub_fails(conn):
    from bridge.publish.publisher import ensure_playlist_row
    from bridge.spotify.client import SpotifyError
    ensure_playlist_row(conn, "discover_weekly", "fixed", "37i9dQZEVXcFakeDw00003")
    ensure_playlist_row(conn, "release_radar", "fixed", "37i9dQZEVXbFakeRr00003")
    rec = Rec()
    await Poller(conn, FakeSpotify(fail=[SpotifyError("down")]), rec, rec, FakeMa(), NO_IDS, lambda: NOW,
                 sleep=nosleep).run_once()
    assert rec.calls == [("discover_weekly", "37i9dQZEVXcFakeDw00003"),
                         ("release_radar", "37i9dQZEVXbFakeRr00003")]


async def test_missing_fixed_slot_is_reported_not_fatal(conn):
    from bridge.health import build_health
    rec = Rec()
    out = await Poller(conn, FakeSpotify(), rec, rec, FakeMa(), NO_IDS, lambda: NOW, sleep=nosleep).run_once()
    assert [slot for slot, _ in rec.calls] == ["daily_mix_1", "daily_mix_2", "daylist"]
    assert out["daily_mix_1"] == "snapshot/published"
    row = conn.execute("SELECT last_state, last_error FROM playlist WHERE slot = 'discover_weekly'").fetchone()
    assert row["last_state"] == "error" and "set discover_weekly_id" in row["last_error"]
    later = NOW + timedelta(hours=3)
    problems = {p["key"]: p["text"] for p in build_health(conn, later, 3600, 10 ** 12, language="en")["problems"]}
    assert "not found in your hub; set release_radar_id" in problems["playlist:release_radar"]
