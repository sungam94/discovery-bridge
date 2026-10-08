from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.queue.client import AdapterError, AdapterRejected
from bridge.queue.runner import QueueRunner
from bridge.repo import set_setting
from bridge.timeutil import iso

T0 = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)   # 14:30 Berlin, afternoon
INTERRUPTED = {"outcome": "interrupted", "verified": False, "reason": None, "interrupt": "pause",
               "progress_ms": 10_000, "observed_track": "sp1", "pause_confirmed": True}
DONE = {"outcome": "done", "verified": True, "reason": None, "interrupt": None, "progress_ms": 235_000,
        "observed_track": "sp1", "pause_confirmed": True}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeAdapter:
    def __init__(self, state="ok", reachable=True):
        self.state, self.reachable, self.jobs, self.calls = state, reachable, {}, []

    async def health(self):
        if not self.reachable:
            raise AdapterError("ConnectError")
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        busy = "busy" if running and self.state == "ok" else self.state
        return {"state": busy, "detail": "", "job_id": running[0]["id"] if running else None}

    async def submit(self, job):
        self.calls.append(("submit", job["id"]))
        self.jobs[job["id"]] = {"id": job["id"], "status": "running", "result": None}
        return self.jobs[job["id"]]

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def pause(self):
        self.calls.append(("pause", None))
        self.state = "paused"
        return {"state": "paused"}

    async def resume(self):
        self.calls.append(("resume", None))
        self.state = "ok"
        return {"state": "ok"}

    def finish(self, job_id, result):
        self.jobs[job_id] = {"id": job_id, "status": "finished", "result": result}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def eligible_event(conn, eid=1):
    conn.execute("INSERT INTO taste_event(id, source, ma_item_uri, provider_item_id, started_at, listened_ms, "
                 "duration_ms, outcome, spotify_id, source_spotify_playlist_id, policy_result) "
                 "VALUES (?, 'ma_play', 'u', ?, ?, 240000, 240000, 'completed', 'sp1', 'dw', 'eligible')",
                 (eid, str(eid), iso(T0)))


def runner(conn, adapter, clock=None):
    return QueueRunner(conn, adapter, clock or Clock(), "Europe/Berlin", 50, 3, 48 * 3600)


def job(conn, jid=1):
    return conn.execute("SELECT * FROM feedback_job WHERE id = ?", (jid,)).fetchone()


def feedback_on(conn):
    set_setting(conn, "feedback_paused", "false")


async def test_runner_starts_next_job(conn):
    feedback_on(conn)
    eligible_event(conn)
    adapter = FakeAdapter()
    assert (await runner(conn, adapter).tick()).startswith("started job")
    assert (job(conn)["status"], job(conn)["attempts"]) == ("running", 1)
    assert adapter.calls == [("submit", 1)]


async def test_runner_applies_finished_result(conn):
    feedback_on(conn)
    eligible_event(conn)
    adapter = FakeAdapter()
    r = runner(conn, adapter)
    await r.tick()
    adapter.finish(1, DONE)
    await r.tick()
    assert job(conn)["status"] == "verified"


async def test_runner_pauses_adapter_when_feedback_paused(conn):
    eligible_event(conn)                           # feedback_paused is seeded true
    adapter = FakeAdapter(state="ok")
    assert await runner(conn, adapter).tick() == "paused adapter"
    assert adapter.state == "paused"
    assert conn.execute("SELECT count(*) FROM feedback_job").fetchone()[0] == 0   # no jobs while paused


async def test_pause_during_job_returns_it_to_pending(conn):
    feedback_on(conn)
    eligible_event(conn)
    adapter = FakeAdapter()
    r = runner(conn, adapter)
    await r.tick()                                 # job 1 starts
    set_setting(conn, "feedback_paused", "true")
    assert await r.tick() == "paused adapter"
    adapter.finish(1, INTERRUPTED)
    await r.tick()
    assert (job(conn)["status"], job(conn)["attempts"]) == ("pending", 0)


async def test_runner_resumes_paused_adapter(conn):
    feedback_on(conn)
    adapter = FakeAdapter(state="paused")
    assert await runner(conn, adapter).tick() == "resumed adapter"
    assert adapter.state == "ok"


async def test_runner_retries_resume_from_error(conn):
    feedback_on(conn)
    clock = Clock()
    adapter = FakeAdapter(state="error")
    r = runner(conn, adapter, clock)
    adapter.resume = _stays(adapter, "error")
    assert await r.tick() == "retried resume"
    assert await r.tick() == "adapter error"       # not again within 15 min
    clock.t += timedelta(minutes=15)
    assert await r.tick() == "retried resume"


def _stays(adapter, state):
    async def resume():
        adapter.calls.append(("resume", None))
        return {"state": state}
    return resume


async def test_runner_survives_unreachable_adapter(conn):
    feedback_on(conn)
    r = runner(conn, FakeAdapter(reachable=False))
    assert await r.tick() == "unreachable"
    assert await r.tick() == "unreachable"
    assert [x[0] for x in conn.execute("SELECT state FROM adapter_health")] == ["unreachable"]


async def test_outage_of_an_hour_loses_running_jobs(conn):
    feedback_on(conn)
    eligible_event(conn)
    clock = Clock()
    adapter = FakeAdapter()
    r = runner(conn, adapter, clock)
    await r.tick()                                 # job 1 running
    adapter.reachable = False
    await r.tick()
    clock.t += timedelta(minutes=61)
    await r.tick()
    assert job(conn)["status"] == "unverified"


async def test_runner_recovers_lost_job_after_restart(conn):
    feedback_on(conn)
    eligible_event(conn)
    await runner(conn, FakeAdapter()).tick()       # job 1 running in the old adapter
    await runner(conn, FakeAdapter()).tick()       # new bridge, new adapter: knows nothing
    assert job(conn)["status"] == "unverified"


async def test_runner_times_out_hung_job(conn):
    feedback_on(conn)
    eligible_event(conn)
    clock = Clock()
    adapter = FakeAdapter()
    r = runner(conn, adapter, clock)
    await r.tick()
    clock.t += timedelta(minutes=20)               # max runtime: 4 min target + 15 min
    await r.tick()
    assert job(conn)["status"] == "unverified" and ("pause", None) in adapter.calls


async def test_rejected_submit_returns_job_without_counting_a_start(conn):
    feedback_on(conn)
    eligible_event(conn)

    class Refusing(FakeAdapter):
        async def submit(self, job):
            raise AdapterRejected("submit HTTP 409")

    assert await runner(conn, Refusing()).tick() == "submit rejected"
    assert (job(conn)["status"], job(conn)["attempts"]) == ("pending", 0)
    assert conn.execute("SELECT count(*) FROM job_start").fetchone()[0] == 0


async def test_lost_submit_answer_is_resolved_on_the_next_tick(conn):
    feedback_on(conn)
    eligible_event(conn)
    eligible_event(conn, 2)

    class Flaky(FakeAdapter):
        def __init__(self, accept):
            super().__init__()
            self.accept = accept

        async def submit(self, job):
            if self.accept:
                await super().submit(job)
            raise AdapterError("ReadTimeout")

    never = Flaky(accept=False)
    r = runner(conn, never)
    assert await r.tick() == "submit uncertain"
    assert job(conn, 1)["attempts"] == 1
    # next tick: the adapter does not know job 1, so it returns to pending without an attempt and is retried
    assert await r.tick() == "submit uncertain"
    assert job(conn, 1)["attempts"] == 1 and conn.execute("SELECT count(*) FROM job_start").fetchone()[0] == 1
    got = Flaky(accept=True)
    r2 = runner(conn, got)                         # a restarted bridge: job 1 is lost, job 2 starts
    await r2.tick()
    await r2.tick()
    assert job(conn, 2)["status"] == "running"     # the adapter has it: it stays running


async def test_owner_listening_elsewhere_does_not_burn_the_cap(conn):
    feedback_on(conn)
    eligible_event(conn)
    elsewhere = {"outcome": "interrupted", "verified": False, "reason": None, "interrupt": "elsewhere",
                 "progress_ms": 0, "observed_track": None, "pause_confirmed": False}

    class Busy(FakeAdapter):
        async def submit(self, job):
            await super().submit(job)
            self.finish(job["id"], elsewhere)

    clock = Clock()
    r = runner(conn, Busy(), clock)
    for _ in range(5):
        await r.tick()
        clock.t += timedelta(seconds=10)
    assert conn.execute("SELECT count(*) FROM job_start").fetchone()[0] == 0
    assert job(conn)["status"] == "pending"