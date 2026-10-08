"""Deezer's public API (no key): the 30 s preview clip of a track by ISRC.

Preview URLs carry a signed token, so they are never logged or put into error texts."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import httpx

API = "https://api.deezer.com/track/isrc:{isrc}"
NO_DATA = 800
TEMPORARY_CODES = {4, 700}  # quota exceeded, service busy


class DeezerUnavailable(Exception):
    """Deezer is unreachable, busy or over quota: try the same track again later."""


class ClipError(Exception):
    """The clip of this track cannot be had; stored as an error."""


class Deezer:
    def __init__(self, http: httpx.Client, min_interval_s: float = 0.25,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._http, self._interval, self._clock, self._sleep = http, min_interval_s, clock, sleep
        self._last: float | None = None

    def _get(self, url: str) -> httpx.Response:
        if self._last is not None:
            wait = self._last + self._interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            r = self._http.get(url, timeout=30, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise DeezerUnavailable(type(exc).__name__) from None
        finally:
            self._last = self._clock()
        if r.status_code == 429 or r.status_code >= 500:
            raise DeezerUnavailable(f"HTTP {r.status_code}")
        return r

    def preview(self, isrc: str) -> str | None:
        return self.lookup(isrc)[0]

    def lookup(self, isrc: str) -> tuple[str | None, dict | None]:
        """(preview URL or None, the track's facts such as bpm, rank and release date; never the preview URL)."""
        # Only a clean JSON answer settles a track: a blocked IP (401, 403), a moved API (404) or a broken body
        # would otherwise turn the whole queue into error rows within minutes.
        r = self._get(API.format(isrc=isrc))
        if r.status_code != 200:
            raise DeezerUnavailable(f"lookup HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise DeezerUnavailable("lookup answer is not JSON") from None
        if not isinstance(body, dict):
            raise DeezerUnavailable("lookup answer is not an object")
        err = body.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            if code in TEMPORARY_CODES:
                raise DeezerUnavailable(f"Deezer error {code}")
            if code == NO_DATA:
                return None, None
            raise ClipError(f"Deezer error {code}")
        return body.get("preview") or None, {k: v for k, v in body.items() if k != "preview"}

    def download(self, url: str, path: Path) -> None:
        r = self._get(url)
        if r.status_code != 200:
            raise ClipError(f"clip HTTP {r.status_code}")
        Path(path).write_bytes(r.content)
