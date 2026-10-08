from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from bridge.models import FetchedPlaylist, SpotifyCandidate, SpotifyTrack
from bridge.spotify.parse import (
    HUB_PAGE_URI, base62_to_gid, parse_made_for_you, parse_playlist_page, parse_search_tracks, parse_spclient_track,
)
from bridge.spotify.session import CookieInvalid, Credentials, SessionError

PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
SPCLIENT_TRACK_URL = "https://spclient.wg.spotify.com/metadata/4/track/{gid}?market=from_token"
PAGE_SIZE = 100
RECAPTURE_STATUSES = {400, 401, 403, 404}
MIN_RECAPTURE_INTERVAL_S = 60


class SpotifyError(Exception):
    pass


class AuthExpired(SpotifyError):
    pass


class RateLimited(SpotifyError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class CredentialSource(Protocol):
    async def capture(self) -> Credentials: ...


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return 30.0


class SpotifyClient:
    def __init__(self, source: CredentialSource, http: httpx.AsyncClient,
                 clock: Callable[[], float] = time.time, refresh_margin_s: float = 300) -> None:
        self._source = source
        self._http = http
        self._clock = clock
        self._margin = refresh_margin_s
        self._creds: Credentials | None = None
        self._captured_at = 0.0
        self._lock = asyncio.Lock()

    async def _capture(self) -> Credentials:
        try:
            creds = await self._source.capture()
        except CookieInvalid as exc:
            raise AuthExpired(str(exc)) from exc
        except SessionError as exc:
            raise SpotifyError(str(exc)) from exc
        except SpotifyError:
            raise
        except Exception as exc:
            raise SpotifyError(f"capture failed: {type(exc).__name__}: {exc}") from exc
        self._creds, self._captured_at = creds, self._clock()
        return creds

    async def _credentials(self, force: bool = False) -> Credentials:
        """Single-flight: a caller that waited for another caller's capture reuses its result."""
        asked_at = self._clock()
        async with self._lock:
            if self._creds is not None and self._captured_at > asked_at:
                return self._creds
            if force or self._creds is None:
                return await self._capture()
            near_expiry = self._creds.expires_at - self._clock() < self._margin
            if near_expiry and self._clock() - self._captured_at > MIN_RECAPTURE_INTERVAL_S:
                return await self._capture()
            return self._creds

    async def _pathfinder(self, op: str, variables: dict[str, Any]) -> dict:
        last_status = None
        for attempt in range(2):
            creds = await self._credentials(force=attempt > 0)
            sha = creds.hashes.get(op)
            if not sha:
                raise SpotifyError(f"no persisted-query hash captured for {op}")
            resp = await self._http.post(PATHFINDER_URL, timeout=30, json={
                "variables": variables, "operationName": op,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            }, headers={
                "authorization": f"Bearer {creds.token}", "app-platform": "WebPlayer",
                "spotify-app-version": creds.app_version, "user-agent": creds.user_agent,
                "content-type": "application/json;charset=UTF-8",
            })
            if resp.status_code == 429:
                raise RateLimited(_retry_after(resp))
            if resp.status_code == 200:
                body = resp.json()
                if not body.get("errors") and body.get("data"):
                    return body
                last_status = 400
                continue
            last_status = resp.status_code
            if resp.status_code not in RECAPTURE_STATUSES:
                break
        if last_status == 401:
            raise AuthExpired(f"{op}: 401 after recapture")
        raise SpotifyError(f"{op}: HTTP {last_status}")

    async def fetch_playlist(self, playlist_id: str) -> FetchedPlaylist:
        tracks: list[SpotifyTrack] = []
        revision = None
        offset = 0
        while True:
            page = parse_playlist_page(await self._pathfinder("fetchPlaylist", {
                "uri": f"spotify:playlist:{playlist_id}", "offset": offset, "limit": PAGE_SIZE,
                "enableWatchFeedEntrypoint": True, "includeEpisodeContentRatingsV2": True,
            }))
            if not page.found:
                raise SpotifyError(f"playlist {playlist_id} not found")
            revision = revision or page.revision_id
            tracks.extend(page.tracks)
            offset += page.raw_count
            if page.raw_count == 0 or offset >= page.total:
                return FetchedPlaylist(tracks, revision)

    async def made_for_you(self) -> dict[str, list[tuple[str, str]]]:
        resp = await self._pathfinder("browsePage", {
            "pagePagination": {"offset": 0, "limit": 20}, "sectionPagination": {"offset": 0, "limit": 20},
            "uri": HUB_PAGE_URI, "browseEndUserIntegration": "INTEGRATION_WEB_PLAYER",
            "includeEpisodeContentRatingsV2": True,
        })
        return parse_made_for_you(resp)

    async def search_isrc(self, isrc: str) -> list[SpotifyCandidate]:
        """Spec §4.5: pathfinder searchTracks for `isrc:<code>`. Variables as recorded in spike check 9."""
        resp = await self._pathfinder("searchTracks", {
            "includePreReleases": False, "includeAlbumPreReleases": False, "numberOfTopResults": 20,
            "searchTerm": f"isrc:{isrc}", "offset": 0, "limit": 20, "includeAudiobooks": True,
            "includeAuthors": True, "includeEpisodeContentRatingsV2": True,
        })
        return parse_search_tracks(resp)

    async def track_metadata(self, track_id: str) -> tuple[str | None, int | None]:
        url = SPCLIENT_TRACK_URL.format(gid=base62_to_gid(track_id))
        last_status = None
        for attempt in range(2):
            creds = await self._credentials(force=attempt > 0)
            resp = await self._http.get(url, timeout=30, headers={
                "authorization": f"Bearer {creds.token}", "accept": "application/json"})
            if resp.status_code == 200:
                return parse_spclient_track(resp.json())
            if resp.status_code == 429:
                raise RateLimited(_retry_after(resp))
            if resp.status_code == 404:
                return None, None
            last_status = resp.status_code
            if resp.status_code not in (401, 403):
                break
        if last_status == 401:
            raise AuthExpired("spclient: 401 after recapture")
        raise SpotifyError(f"spclient track {track_id}: HTTP {last_status}")
