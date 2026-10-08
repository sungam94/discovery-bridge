"""Pure parsers for Spotify web-player responses. Shapes: the captures in tests/fixtures/spike/."""
from __future__ import annotations

from dataclasses import dataclass

from bridge.models import SpotifyCandidate, SpotifyTrack

HUB_PAGE_URI = "spotify:page:0JQ5DAt0tbjZptfcdMSKl3"
SECTION_DAILY_MIXES = "spotify:section:0JQ5DACFo5h0jxzOyHOsIo"
SECTION_JUST_FOR_YOU = "spotify:section:0JQ5DACFo5h0jxzOyHOsIe"
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class PlaylistPage:
    tracks: list[SpotifyTrack]
    total: int
    raw_count: int
    revision_id: str | None
    found: bool


def base62_to_gid(b62: str) -> str:
    n = 0
    for ch in b62:
        n = n * 62 + _B62.index(ch)
    return f"{n:032x}"


def parse_playlist_page(resp: dict) -> PlaylistPage:
    pl = ((resp or {}).get("data") or {}).get("playlistV2") or {}
    if pl.get("__typename", "Playlist") != "Playlist" or "content" not in pl:
        return PlaylistPage([], 0, 0, None, False)
    content = pl["content"]
    items = content.get("items") or []
    tracks: list[SpotifyTrack] = []
    for item in items:
        data = ((item or {}).get("itemV2") or {}).get("data") or {}
        uri = data.get("uri") or ""
        if data.get("__typename") != "Track" or not uri.startswith("spotify:track:"):
            continue
        tracks.append(SpotifyTrack(
            id=uri.rsplit(":", 1)[1],
            name=data.get("name", ""),
            artists=tuple(a["profile"]["name"] for a in (data.get("artists") or {}).get("items", [])),
            album=(data.get("albumOfTrack") or {}).get("name", ""),
            duration_ms=int((data.get("trackDuration") or {}).get("totalMilliseconds", 0)),
            explicit=(data.get("contentRating") or {}).get("label") == "EXPLICIT",
        ))
    return PlaylistPage(tracks, int(content.get("totalCount", len(items))), len(items),
                        pl.get("revisionId"), True)


def parse_made_for_you(resp: dict) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for sec in resp["data"]["browse"]["sections"]["items"]:
        entries = []
        for it in (sec.get("sectionItems") or {}).get("items", []):
            d = ((it or {}).get("content") or {}).get("data") or {}
            uri = d.get("uri") or ""
            if d.get("__typename") == "Playlist" and uri.startswith("spotify:playlist:"):
                entries.append((uri.rsplit(":", 1)[1], d.get("name", "")))
        out[sec.get("uri", "")] = entries
    return out


# Every account has its own Discover Weekly and Release Radar, but their IDs share these prefixes
FIXED_PREFIXES = {"discover_weekly": "37i9dQZEVXc", "release_radar": "37i9dQZEVXb"}


def fixed_playlist_ids(sections: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """Discover Weekly and Release Radar of the logged-in account, found anywhere in the hub. A slot is left out
    unless exactly one playlist ID has its prefix."""
    ids = {pid for items in sections.values() for pid, _ in items}
    out = {}
    for slot, prefix in FIXED_PREFIXES.items():
        found = [pid for pid in ids if pid.startswith(prefix)]
        if len(found) == 1:
            out[slot] = found[0]
    return out


def daily_mix_ids(sections: dict[str, list[tuple[str, str]]]) -> list[str]:
    return [pid for pid, _ in sections.get(SECTION_DAILY_MIXES, [])]


# The same ID for every account (Spotify resolves it to the logged-in user's daylist); the name follows the
# daylist's mood, so the name match below is only the fallback
DAYLIST_ID = "37i9dQZF1EP6YuccBxUcC1"


def daylist_id(sections: dict[str, list[tuple[str, str]]]) -> str | None:
    items = sections.get(SECTION_JUST_FOR_YOU, [])
    if any(pid == DAYLIST_ID for pid, _ in items):
        return DAYLIST_ID
    return next((pid for pid, name in items if name.strip().lower().startswith("daylist")), None)


def parse_spclient_track(resp: dict) -> tuple[str | None, int | None]:
    isrc = next((e.get("id") for e in resp.get("external_id", []) if e.get("type") == "isrc"), None)
    duration = resp.get("duration")
    return (isrc.upper() if isrc else None), (int(duration) if duration is not None else None)


def parse_search_tracks(resp: dict) -> list[SpotifyCandidate]:
    """Pathfinder searchTracks (spike check 9). Results carry no album release date."""
    items = (((resp.get("data") or {}).get("searchV2") or {}).get("tracksV2") or {}).get("items") or []
    out: list[SpotifyCandidate] = []
    for it in items:
        d = ((it or {}).get("item") or {}).get("data") or {}
        if d.get("__typename") != "Track" or not d.get("id"):
            continue
        out.append(SpotifyCandidate(
            id=d["id"], name=d.get("name", ""),
            duration_ms=int(((d.get("duration") or {}).get("totalMilliseconds")) or 0),
            explicit=((d.get("contentRating") or {}).get("label") == "EXPLICIT"),
            album=(d.get("albumOfTrack") or {}).get("name", ""),
        ))
    return out
