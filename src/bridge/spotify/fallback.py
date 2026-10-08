"""Spec §3.1: two token providers. Headless session first; the adapter's web-player token as fallback."""
from __future__ import annotations

import logging
from collections.abc import Callable

from bridge.spotify.session import Credentials, SessionError

log = logging.getLogger("bridge.spotify")


class FallbackSource:
    def __init__(self, primary, adapter, load_meta: Callable[[], dict | None],
                 save_meta: Callable[[dict], None]) -> None:
        self._primary, self._adapter, self._load, self._save = primary, adapter, load_meta, save_meta

    async def capture(self) -> Credentials:
        try:
            creds = await self._primary.capture()
        except SessionError as exc:
            meta = self._load()
            if self._adapter is None or not meta:
                raise
            try:
                tok = await self._adapter.token()
            except Exception:
                raise exc from None
            log.warning("headless capture failed (%s); using the adapter's token", type(exc).__name__)
            return Credentials(token=tok["accessToken"], expires_at=tok["accessTokenExpirationTimestampMs"] / 1000,
                               app_version=meta["app_version"], user_agent=meta["user_agent"],
                               hashes=dict(meta["hashes"]))
        self._save({"app_version": creds.app_version, "user_agent": creds.user_agent, "hashes": dict(creds.hashes)})
        return creds
