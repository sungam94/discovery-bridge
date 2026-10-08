"""HTTP client for the adapter API (spec §2, §7): localhost, bearer token."""
from __future__ import annotations

import httpx


class AdapterError(Exception):
    """Transport error or server error: the adapter's view is unknown."""


class AdapterRejected(AdapterError):
    """The adapter answered with a 4xx: it did not accept the request."""


class AdapterClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient, timeout_s: float = 30) -> None:
        self._base, self._headers, self._http, self._timeout = base_url.rstrip("/"), \
            {"Authorization": f"Bearer {token}"}, http, timeout_s

    async def _req(self, method: str, path: str, ok=(200,), **kw) -> httpx.Response:
        try:
            r = await self._http.request(method, self._base + path, headers=self._headers, timeout=self._timeout, **kw)
        except httpx.HTTPError as exc:
            raise AdapterError(f"{method} {path}: {type(exc).__name__}") from exc
        if r.status_code in ok:
            return r
        if 400 <= r.status_code < 500:
            raise AdapterRejected(f"{method} {path}: HTTP {r.status_code}")
        raise AdapterError(f"{method} {path}: HTTP {r.status_code}")

    async def health(self) -> dict:
        return (await self._req("GET", "/health")).json()

    async def submit(self, job: dict) -> dict:
        return (await self._req("POST", "/jobs", ok=(200, 202), json=job)).json()

    async def get_job(self, job_id: int) -> dict | None:
        r = await self._req("GET", f"/jobs/{job_id}", ok=(200, 404))
        return None if r.status_code == 404 else r.json()

    async def pause(self) -> dict:
        return (await self._req("POST", "/pause")).json()

    async def resume(self) -> dict:
        return (await self._req("POST", "/resume")).json()

    async def token(self) -> dict:
        return (await self._req("GET", "/token")).json()
