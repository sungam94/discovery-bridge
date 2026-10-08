"""Adapter service and HTTP API (spec §2, §6.4, §7): localhost only, bearer token, one job at a time, starts paused.
The idle watchdog pauses any playback on the adapter's own device while no job runs."""
from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("adapter")
SAVE_TIMEOUT_S = 300
PLAY_TIMEOUT_MARGIN_S = 120
TOKEN_REFRESH_EVERY_S = 300


def _failed(reason: str) -> dict:
    return {"outcome": "failed", "verified": False, "reason": reason, "interrupt": None, "progress_ms": 0,
            "observed_track": None, "pause_confirmed": False}


class AdapterService:
    def __init__(self, driver, runner, logged_in: Callable[[], Awaitable[bool]],
                 token: Callable[[], Awaitable[dict | None]],
                 refresh_token: Callable[[], Awaitable[None]] | None = None, ad_limit_s: float = 600.0) -> None:
        self.driver, self.runner, self._logged_in, self._token = driver, runner, logged_in, token
        self._refresh_token, self._ad_limit_s = refresh_token, ad_limit_s
        self.state, self.detail = "paused", "boot"
        self.jobs: dict[int, dict] = {}
        self.current: int | None = None

    def mark_error(self, detail: str) -> None:
        self.state, self.detail = "error", detail[:300]

    def health(self) -> dict:
        state = self.state
        if self.current is not None and state == "ok":
            _, st = self.driver.state()
            state = "ad" if st is not None and st.is_ad else "busy"
        return {"state": state, "job_id": self.current, "detail": self.detail}

    def _timeout(self, job: dict) -> float:
        if job["kind"] == "save":
            return SAVE_TIMEOUT_S
        return (job.get("target_ms") or 0) / 1000 + self._ad_limit_s + PLAY_TIMEOUT_MARGIN_S

    async def _run(self, job: dict) -> None:
        try:
            result = await asyncio.wait_for(self.runner.run(job), self._timeout(job))
        except TimeoutError:
            log.error("job %s timed out", job["id"])
            result = _failed("timeout")
            with suppress(Exception):
                await self.driver.pause()
        except Exception as exc:  # the driver failed: report, pause, never crash the API
            log.exception("job %s failed", job["id"])
            result = _failed(f"error: {type(exc).__name__}")
            self.mark_error(f"{type(exc).__name__}: {exc}")
            with suppress(Exception):
                await self.driver.pause()
        self.jobs[job["id"]] = {"id": job["id"], "status": "finished", "result": result}
        self.current = None

    def submit(self, job: dict) -> tuple[int, dict]:
        rec = self.jobs.get(job["id"])
        if rec is not None and rec["status"] == "running":
            return 200, rec
        if self.state != "ok" or self.current is not None:
            return 409, {"state": self.health()["state"]}
        self.jobs[job["id"]] = {"id": job["id"], "status": "running", "result": None}
        self.current = job["id"]
        self.runner.pause_requested.clear()
        asyncio.get_running_loop().create_task(self._run(job))
        return 202, {"id": job["id"], "status": "running"}

    def pause(self) -> dict:
        self.state, self.detail = "paused", "paused by bridge"
        if self.current is not None:
            self.runner.pause_requested.set()
        else:
            asyncio.get_running_loop().create_task(self.watchdog_once())
        return self.health()

    async def resume(self) -> dict:
        try:
            ok = await self._logged_in()
        except Exception as exc:
            self.mark_error(f"login check failed: {type(exc).__name__}")
            return self.health()
        if ok:
            self.state, self.detail = "ok", ""
        else:
            self.state, self.detail = "auth_expired", "web player session is anonymous"
        return self.health()

    async def watchdog_once(self) -> None:
        if self.current is not None:
            return
        _, st = self.driver.state()
        own = self.driver.own_device_ids
        if not own:  # until our device has reported its state, playback may belong to another device
            return
        if st is not None and not st.is_paused and st.is_playing and (st.active_device_id is None
                                                                       or st.active_device_id in own):
            log.warning("watchdog: pausing playback outside a job (track %s)", st.track_id)
            with suppress(Exception):
                await self.driver.pause()

    async def run_background(self, every_s: float = 2.0) -> None:
        since_refresh = 0.0
        while True:
            await self.watchdog_once()
            since_refresh += every_s
            if self._refresh_token is not None and self.current is None and since_refresh >= TOKEN_REFRESH_EVERY_S:
                since_refresh = 0.0
                with suppress(Exception):
                    await self._refresh_token()
                await self.watchdog_once()
            await asyncio.sleep(every_s)


def create_app(service: AdapterService, api_token: str, background: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.get_running_loop().create_task(service.run_background()) if background else None
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def auth(request: Request) -> None:
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {api_token}".encode()):
            raise HTTPException(status_code=401)

    @app.post("/jobs", dependencies=[Depends(auth)])
    async def post_job(job: dict):
        code, body = service.submit(job)
        return JSONResponse(body, status_code=code)

    @app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
    async def get_job(job_id: int):
        if job_id not in service.jobs:
            raise HTTPException(status_code=404)
        return service.jobs[job_id]

    @app.get("/health", dependencies=[Depends(auth)])
    async def health():
        return service.health()

    @app.post("/pause", dependencies=[Depends(auth)])
    async def pause():
        return service.pause()

    @app.post("/resume", dependencies=[Depends(auth)])
    async def resume():
        return await service.resume()

    @app.get("/token", dependencies=[Depends(auth)])
    async def token():
        tok = await service._token()
        if not tok:
            raise HTTPException(status_code=503)
        return tok

    return app
