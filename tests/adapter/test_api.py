import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from adapter.api import AdapterService, create_app
from adapter.state import PlayerState

AUTH = {"Authorization": "Bearer secret"}
DONE = {"outcome": "done", "verified": True, "reason": None, "interrupt": None, "progress_ms": 130_000,
        "observed_track": "t", "pause_confirmed": True}
JOB = {"id": 7, "kind": "play", "spotify_id": "t", "target_ms": 130_000, "source_spotify_playlist_id": None}


class FakeRunner:
    """Finishes a job only when the test releases it; records the pause flag at start."""

    def __init__(self, fail=None):
        self.pause_requested, self.release, self.jobs, self.flag_at_start, self.fail = \
            asyncio.Event(), None, [], [], fail

    async def run(self, job):
        self.jobs.append(job["id"])
        self.flag_at_start.append(self.pause_requested.is_set())
        if self.fail:
            raise self.fail
        while not self.release:
            await asyncio.sleep(0.01)
        return DONE


class FakeDriver:
    def __init__(self, playing=False, device="me"):
        self.own_device_ids = {"me"}
        self.pauses, self.st = 0, PlayerState("t", None, not playing, playing, 1, 200_000, None, False, device)

    def state(self):
        return 1, self.st

    async def pause(self):
        self.pauses += 1
        return True


def make(runner=None, driver=None, logged_in=True, token=None):
    async def li():
        return logged_in

    async def tok():
        return token

    return AdapterService(driver=driver or FakeDriver(), runner=runner or FakeRunner(), logged_in=li, token=tok)


def wait_finished(c, jid):
    for _ in range(200):
        r = c.get(f"/jobs/{jid}", headers=AUTH).json()
        if r["status"] == "finished":
            return r
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_auth_required():
    with TestClient(create_app(make(), "secret")) as c:
        assert c.get("/health").status_code == 401
        assert c.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_starts_paused_and_rejects_jobs():
    with TestClient(create_app(make(), "secret")) as c:
        assert c.get("/health", headers=AUTH).json()["state"] == "paused"
        assert c.post("/jobs", json=JOB, headers=AUTH).status_code == 409


def test_run_job_idempotent_while_running_and_retry_after_finish():
    runner = FakeRunner()
    with TestClient(create_app(make(runner), "secret")) as c:
        assert c.post("/resume", headers=AUTH).json()["state"] == "ok"
        assert c.post("/jobs", json=JOB, headers=AUTH).status_code == 202
        assert c.get("/health", headers=AUTH).json()["state"] == "busy"
        assert c.post("/jobs", json=JOB, headers=AUTH).status_code == 200      # same id while running
        runner.release = True
        assert wait_finished(c, 7)["result"]["outcome"] == "done"
        assert c.get("/health", headers=AUTH).json()["state"] == "ok"
        assert c.post("/jobs", json=JOB, headers=AUTH).status_code == 202      # a retry runs again
        wait_finished(c, 7)
        assert runner.jobs == [7, 7]
        assert c.get("/jobs/99", headers=AUTH).status_code == 404


def test_pause_interrupts_and_flag_is_cleared_before_a_new_job():
    runner = FakeRunner()
    svc = make(runner)
    with TestClient(create_app(svc, "secret")) as c:
        c.post("/resume", headers=AUTH)
        runner.pause_requested.set()                                           # left over from an earlier job
        c.post("/jobs", json=JOB, headers=AUTH)
        for _ in range(100):
            if runner.flag_at_start:
                break
            time.sleep(0.01)
        assert runner.flag_at_start == [False]
        assert c.post("/pause", headers=AUTH).json()["state"] == "paused"
        assert runner.pause_requested.is_set()
        runner.release = True


def test_runner_exception_sets_error_and_pauses():
    driver = FakeDriver(playing=True)
    with TestClient(create_app(make(FakeRunner(fail=RuntimeError("page crashed")), driver), "secret")) as c:
        c.post("/resume", headers=AUTH)
        c.post("/jobs", json=JOB, headers=AUTH)
        r = wait_finished(c, 7)
        assert r["result"]["reason"] == "error: RuntimeError" and driver.pauses == 1
        assert c.get("/health", headers=AUTH).json()["state"] == "error"
        assert c.post("/resume", headers=AUTH).json()["state"] == "ok"      # resume clears the error


def test_resume_without_login_is_auth_expired():
    with TestClient(create_app(make(logged_in=False), "secret")) as c:
        assert c.post("/resume", headers=AUTH).json()["state"] == "auth_expired"


def test_token():
    with TestClient(create_app(make(token={"accessToken": "x", "accessTokenExpirationTimestampMs": 1}), "secret")) as c:
        assert c.get("/token", headers=AUTH).json()["accessToken"] == "x"
    with TestClient(create_app(make(token=None), "secret")) as c:
        assert c.get("/token", headers=AUTH).status_code == 503


async def test_watchdog_pauses_unsupervised_playback():
    playing = FakeDriver(playing=True)
    await make(driver=playing).watchdog_once()
    assert playing.pauses == 1
    other = FakeDriver(playing=True, device="phone")
    await make(driver=other).watchdog_once()
    assert other.pauses == 0                       # never touch another device of the account
    idle = FakeDriver(playing=False)
    await make(driver=idle).watchdog_once()
    assert idle.pauses == 0
    busy = make(driver=FakeDriver(playing=True))
    busy.current = 3                               # a job runs: the runner owns the player
    await busy.watchdog_once()
    assert busy.driver.pauses == 0
    unknown = FakeDriver(playing=True)
    unknown.own_device_ids = set()                 # own device not learned yet: touch nothing
    await make(driver=unknown).watchdog_once()
    assert unknown.pauses == 0
