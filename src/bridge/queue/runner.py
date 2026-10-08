"""Spec §6.2/§6.4 (amended): the feedback queue loop. Creates and expires jobs, keeps the adapter paused or
resumed, starts one job at a time, applies its result, and never lets a job hang."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from bridge.queue.client import AdapterError, AdapterRejected
from bridge.queue.jobs import create_jobs
from bridge.queue.results import apply_result, lose_job, mark_started, max_runtime, recover_running
from bridge.queue.schedule import expire_jobs, next_job
from bridge.repo import is_paused
from bridge.timeutil import iso, parse_iso

log = logging.getLogger("bridge.queue")
BUSY_STATES = ("ok", "busy", "ad")
OUTAGE_LIMIT = timedelta(hours=1)
RESUME_RETRY = timedelta(minutes=15)


class QueueRunner:
    def __init__(self, conn: sqlite3.Connection, client, now: Callable[[], datetime], tz: str,
                 max_jobs_per_day: int, job_attempts: int, max_job_age_s: int, bucket_hours: int = 1,
                 max_play_age_s: int = 14 * 86400) -> None:
        self._conn, self._client, self._now, self._tz = conn, client, now, tz
        self._cap, self._attempts, self._max_age = max_jobs_per_day, job_attempts, max_job_age_s
        self._bucket_hours, self._max_play_age = bucket_hours, timedelta(seconds=max_play_age_s)
        self._recovered = False
        self._unreachable_since: datetime | None = None
        self._last_resume: datetime | None = None
        self._uncertain: set[int] = set()   # jobs whose submit answer was lost
        self.wake = asyncio.Event()
        last = conn.execute("SELECT state FROM adapter_health ORDER BY id DESC LIMIT 1").fetchone()
        self._last_state = last["state"] if last else None

    def _health(self, state: str, detail: str = "") -> None:
        if state != self._last_state:
            self._conn.execute("INSERT INTO adapter_health(ts, state, detail) VALUES (?, ?, ?)",
                               (iso(self._now()), state, detail[:300]))
            self._last_state = state

    def _running(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM feedback_job WHERE status = 'running'").fetchone()

    def _unstart(self, job_id: int) -> None:
        self._conn.execute("UPDATE feedback_job SET status = 'pending', attempts = attempts - 1, started_at = NULL "
                           "WHERE id = ?", (job_id,))
        self._conn.execute("DELETE FROM job_start WHERE id = (SELECT max(id) FROM job_start WHERE job_id = ?)",
                           (job_id,))

    async def tick(self) -> str:
        now = self._now()
        paused = is_paused(self._conn, "feedback_paused")
        if not paused:
            create_jobs(self._conn, now, self._max_age)
        expire_jobs(self._conn, now, self._tz, self._max_age, self._max_play_age)
        try:
            h = await self._client.health()
        except AdapterError as exc:
            self._health("unreachable", str(exc))
            self._unreachable_since = self._unreachable_since or now
            if now - self._unreachable_since >= OUTAGE_LIMIT and (job := self._running()) is not None:
                lose_job(self._conn, job["id"], now, "adapter unreachable for 1 h")
            return "unreachable"
        self._unreachable_since = None
        state = h.get("state", "error")
        self._health(state, h.get("detail") or "")
        try:
            return await self._step(now, paused, state, h)
        except AdapterError as exc:
            self._health("unreachable", str(exc))
            return "adapter call failed"

    async def _step(self, now: datetime, paused: bool, state: str, h: dict) -> str:
        if not self._recovered:
            ids = [r["id"] for r in self._conn.execute("SELECT id FROM feedback_job WHERE status = 'running'")]
            recover_running(self._conn, {i: await self._client.get_job(i) for i in ids}, now, self._attempts)
            self._recovered = True
        adapter_job = h.get("job_id")
        if adapter_job is not None:
            self._conn.execute("UPDATE feedback_job SET status = 'running' WHERE id = ? AND status = 'pending'",
                               (adapter_job,))
        job = self._running()
        if job is not None:
            rec = await self._client.get_job(job["id"])
            if rec is not None:
                self._uncertain.discard(job["id"])     # the adapter has it
            if rec is None:
                if job["id"] in self._uncertain:
                    self._unstart(job["id"])              # the submit never reached the adapter
                else:
                    lose_job(self._conn, job["id"], now, "unknown to the adapter")
                job = None
            elif rec.get("status") == "finished":
                apply_result(self._conn, job["id"], rec["result"], now, self._attempts)
                job = None
            elif now - parse_iso(job["started_at"]) > max_runtime(job):
                await self._client.pause()
                lose_job(self._conn, job["id"], now, "timeout")
                job = None
        if paused:
            if state in BUSY_STATES:
                await self._client.pause()
                return "paused adapter"
            return "feedback paused"
        if state == "paused":
            await self._client.resume()
            return "resumed adapter"
        if state in ("auth_expired", "error"):
            if self._last_resume is None or now - self._last_resume >= RESUME_RETRY:
                self._last_resume = now
                await self._client.resume()
                return "retried resume"
            return f"adapter {state}"
        if job is not None:
            return "job running"
        if state != "ok":
            return f"adapter {state}"
        nxt = next_job(self._conn, now, self._tz, self._cap, self._bucket_hours, self._max_play_age)
        if nxt is None:
            return "idle"
        mark_started(self._conn, nxt["id"], now)
        payload = {"id": nxt["id"], "kind": nxt["kind"], "spotify_id": nxt["spotify_id"], "target_ms": nxt["target_ms"],
                   "source_spotify_playlist_id": nxt["source_spotify_playlist_id"]}
        try:
            await self._client.submit(payload)
        except AdapterRejected as exc:
            log.warning("queue: adapter rejected job %s: %s", nxt["id"], exc)
            self._unstart(nxt["id"])
            return "submit rejected"
        except AdapterError as exc:
            log.warning("queue: submit of job %s uncertain: %s", nxt["id"], exc)
            self._uncertain.add(nxt["id"])
            return "submit uncertain"
        return f"started job {nxt['id']}"

    async def run(self, every_s: float = 10) -> None:
        last = None
        while True:
            try:
                what = await self.tick()
                if what != last:
                    log.info("queue: %s", what)
                    last = what
            except Exception:
                log.exception("queue tick failed")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), every_s)
            except TimeoutError:
                pass
