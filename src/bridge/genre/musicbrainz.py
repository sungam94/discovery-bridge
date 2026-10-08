"""MusicBrainz lookup of an artist's genres. No account needed; at most one request per second."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from bridge.genre.store import artist_key

BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "discovery-bridge/1.0 (private Music Assistant bridge)"


def user_agent(contact: str | None = None) -> str:
    """MusicBrainz and Wikipedia ask clients for contact details; contact_email in config.yaml adds them."""
    return f"{USER_AGENT[:-1]}; {contact})" if contact else USER_AGENT
PAUSE_S = 1.1
ATTEMPTS = 4


class MusicBrainzError(Exception):
    pass


class MusicBrainz:
    def __init__(self, http: httpx.AsyncClient, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 contact: str | None = None) -> None:
        self._http, self._sleep = http, sleep
        self._agent = user_agent(contact)
        self._lock = asyncio.Lock()  # shared by the genre and artist-info workers: one request at a time

    async def _get(self, path: str, params: dict) -> dict:
        async with self._lock:
            return await self._get_unlocked(path, params)

    async def _get_unlocked(self, path: str, params: dict) -> dict:
        last = "no attempt"
        for attempt in range(ATTEMPTS):
            await self._sleep(PAUSE_S * (attempt + 1))
            try:
                resp = await self._http.get(f"{BASE}{path}", params={**params, "fmt": "json"},
                                            headers={"User-Agent": self._agent}, timeout=20)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                continue
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}"
            if resp.status_code not in (429, 503):
                break
        raise MusicBrainzError(f"{path}: {last}")

    async def artist(self, name: str) -> tuple[str | None, list[tuple[str, int]]]:
        """(MusicBrainz ID, genres by votes) of the artist with exactly this name; (None, []) when there is none."""
        found = await self._get("/artist/", {"query": 'artist:"%s"' % name.replace('"', ' '), "limit": 5})
        match = next((a for a in found.get("artists") or [] if artist_key(a.get("name", "")) == artist_key(name)), None)
        if match is None:
            return None, []
        detail = await self._get(f"/artist/{match['id']}", {"inc": "genres"})
        genres = sorted(((g["name"], int(g.get("count", 0))) for g in detail.get("genres") or []),
                        key=lambda g: (-g[1], g[0]))
        return match["id"], genres

    async def details(self, mbid: str) -> dict:
        """Type (Group/Person), start year, place, current members and the Wikidata id of an artist."""
        d = await self._get(f"/artist/{mbid}", {"inc": "artist-rels+url-rels"})
        members = [r["artist"]["name"] for r in d.get("relations") or []
                   if r.get("type") == "member of band" and r.get("direction") == "backward"
                   and not r.get("ended") and (r.get("artist") or {}).get("name")]
        wikidata = next((r["url"]["resource"].rstrip("/").rsplit("/", 1)[-1] for r in d.get("relations") or []
                         if r.get("type") == "wikidata" and (r.get("url") or {}).get("resource")), None)
        begin = ((d.get("life-span") or {}).get("begin") or "")[:4] or None
        area = (d.get("begin-area") or {}).get("name") or (d.get("area") or {}).get("name")
        return {"type": d.get("type"), "begin": begin, "area": area, "members": members, "wikidata": wikidata,
                "raw": d}
