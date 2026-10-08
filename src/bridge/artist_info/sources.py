"""Last.fm and Wikipedia lookups for artist pages (MusicBrainz lives in bridge.genre.musicbrainz)."""
from __future__ import annotations

import httpx

from bridge.artist_info.compose import clean_lastfm_bio
from bridge.genre.musicbrainz import USER_AGENT, user_agent

LASTFM = "https://ws.audioscrobbler.com/2.0/"
WIKIDATA = "https://www.wikidata.org/w/api.php"
WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
NOT_TAGS = {"seen live", "favorites", "favourite", "albums i own"}


class SourceError(Exception):
    pass


async def _get_json(http: httpx.AsyncClient, url: str, params: dict, agent: str = USER_AGENT) -> dict:
    try:
        resp = await http.get(url, params=params, headers={"User-Agent": agent}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"{url.split('/')[2]}: {type(exc).__name__}") from None  # no URL: it may carry a key
    if not isinstance(data, dict):
        raise SourceError(f"{url.split('/')[2]}: unexpected answer")
    return data


class LastFm:
    def __init__(self, http: httpx.AsyncClient, api_key: str | None) -> None:
        self._http, self._key = http, api_key

    async def artist(self, name: str) -> dict | None:
        """Bio, listener count, top tags and similar artists; None without a key or for an unknown artist."""
        if not self._key:
            return None
        d = await _get_json(self._http, LASTFM, {"method": "artist.getinfo", "artist": name, "api_key": self._key,
                                                 "format": "json", "autocorrect": "1"})
        if "error" in d:
            if d.get("error") == 6:
                return None
            raise SourceError(f"last.fm error {d.get('error')}")
        a = d.get("artist") or {}
        try:
            listeners = int((a.get("stats") or {}).get("listeners") or 0)
        except ValueError:
            listeners = 0
        tags = [t["name"] for t in (a.get("tags") or {}).get("tag") or [] if t.get("name", "").lower() not in NOT_TAGS]
        similar = [s["name"] for s in (a.get("similar") or {}).get("artist") or [] if s.get("name")]
        return {"bio": clean_lastfm_bio((a.get("bio") or {}).get("content")), "listeners": listeners,
                "tags": tags[:3], "similar": similar[:5], "raw": a}


class Wikipedia:
    def __init__(self, http: httpx.AsyncClient, contact: str | None = None) -> None:
        self._http = http
        self._agent = user_agent(contact)

    async def intro(self, wikidata_id: str) -> str | None:
        """The lead section of the English article linked from the Wikidata item."""
        ent = await _get_json(self._http, WIKIDATA, {"action": "wbgetentities", "ids": wikidata_id,
                                                     "props": "sitelinks", "sitefilter": "enwiki", "format": "json"},
                               self._agent)
        title = (((ent.get("entities") or {}).get(wikidata_id) or {}).get("sitelinks") or {}).get("enwiki", {}).get("title")
        if not title:
            return None
        q = await _get_json(self._http, WIKIPEDIA, {"action": "query", "prop": "extracts", "exintro": "true",
                                                    "explaintext": "true", "redirects": "1", "titles": title,
                                                    "format": "json"}, self._agent)
        pages = (q.get("query") or {}).get("pages") or {}
        extract = next(iter(pages.values()), {}).get("extract") if pages else None
        return extract.strip() if isinstance(extract, str) and extract.strip() else None
