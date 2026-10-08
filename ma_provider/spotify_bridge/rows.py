"""Select and order the bridge's playlists for the Discover rows. Pure; no Music Assistant imports."""
from __future__ import annotations

import copy
import dataclasses
from collections import Counter
from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")

CURRENT_ORDER = ["Spotify · Discover Weekly", "Spotify · Release Radar", "Spotify · daylist"] + [
    f"Spotify · Daily Mix {i}" for i in range(1, 7)]


def current_rows(playlists: Iterable[T], name: Callable[[T], str] = lambda p: p.name) -> list[T]:
    by_name = {name(p): p for p in playlists}
    return [by_name[n] for n in CURRENT_ORDER if n in by_name]


LAYOUT_VERSION = 1


def layout_rows(layout: dict | None, playlists: Iterable[T],
                name: Callable[[T], str] = lambda p: p.name) -> list[tuple[str, str, list[T]]] | None:
    """Rows from the bridge's layout.json; None when there is no usable layout (use the fallback rows)."""
    if not isinstance(layout, dict) or layout.get("version") != LAYOUT_VERSION:
        return None
    by_name = {name(p): p for p in playlists}
    out = []
    for row in layout.get("rows", []):
        found = [by_name[n] for n in row.get("playlists", []) if n in by_name]
        if found:
            out.append((str(row["id"]), str(row["title"]), with_subtitles(found, row.get("subtitles") or {}, name)))
    return out


def with_subtitles(playlists: list[T], subtitles: dict, name: Callable[[T], str] = lambda p: p.name) -> list[T]:
    """Copies with `owner` replaced: MA shows a playlist card's owner as its subtitle ("Music Assistant" otherwise)."""
    out = []
    for p in playlists:
        sub = subtitles.get(name(p))
        if sub:
            p = copy.copy(p)
            p.owner = sub
        out.append(p)
    return out


NO_LOGO_DOMAIN = "spotify_bridge_no_logo"  # a domain MA's web UI does not know, so its cards draw no source logo


def without_source_logo(items: list[T]) -> list[T]:
    """Copies whose provider mappings name an unknown domain. MA's web UI puts the source's logo (for these
    playlists: Music Assistant itself) on every playlist card; availability and playback use the provider
    instance and item id, which stay unchanged."""
    out = []
    for it in items:
        maps = getattr(it, "provider_mappings", None)
        if maps:
            it = copy.copy(it)
            it.provider_mappings = {dataclasses.replace(m, provider_domain=NO_LOGO_DOMAIN) for m in maps}
        out.append(it)
    return out


SUBTITLE_MAX = 60
TOP_ARTISTS = 3


def top_artists(track_artists: Iterable[list[str]], n: int = TOP_ARTISTS) -> str | None:
    """The n most frequent artists, ties broken by first appearance."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for names in track_artists:
        for a in names:
            counts[a] += 1
            first.setdefault(a, len(first))
    top = sorted(counts, key=lambda a: (-counts[a], first[a]))[:n]
    return ", ".join(top) or None


def _domain(provider: str) -> str:
    return str(provider or "").split("--", 1)[0]


def enrich(items: list, cached: Callable[[str], str | None]) -> tuple[list, list[str]]:
    """Discover cards show a playlist's owner as subtitle. SoundCloud system playlists have no owner but list their
    artists in the description; TIDAL shows "Tidal", so its subtitle comes from cached top artists. Returns copies
    (MA's own objects stay untouched) and the TIDAL playlist URIs whose artists are not cached yet."""
    out, todo = [], []
    for it in items:
        if getattr(it, "media_type", None) != "playlist":
            out.append(it)
            continue
        domain = _domain(getattr(it, "provider", ""))
        sub = None
        if domain == "soundcloud" and not getattr(it, "owner", None):
            desc = (getattr(getattr(it, "metadata", None), "description", None) or "").strip()
            sub = desc[:SUBTITLE_MAX].rstrip(", ") if desc else None
        elif domain == "tidal":
            sub = cached(it.uri)
            if sub is None:
                todo.append(it.uri)
        if sub:
            it = copy.copy(it)
            it.owner = sub
        out.append(it)
    return out, todo


ARTIST_FILE_VERSION = 1


def artist_text(data: dict | None, name: str) -> str | None:
    """The bridge's text for an artist page (artist_info.json), matched by name like the bridge does."""
    if not isinstance(data, dict) or data.get("version") != ARTIST_FILE_VERSION:
        return None
    text = (data.get("artists") or {}).get(" ".join(str(name).casefold().split()))
    return text if isinstance(text, str) and text else None


def _thumb_of(item):
    """(image, where) of an item's thumb: where is "image" for an ItemMapping, "metadata" for a full item."""
    image = getattr(item, "image", None)
    if image is not None:
        return image, "image"
    for img in getattr(getattr(item, "metadata", None), "images", None) or []:
        if getattr(getattr(img, "type", None), "value", getattr(img, "type", None)) == "thumb":
            return img, "metadata"
    return None, None


def stale_generated(items: list, provider: str, exists) -> list[int]:
    """Indexes of playlists whose thumb is a file MA's cover generator (`provider`) has since replaced: MA's
    Recently played keeps the file of the moment the playlist was played, and a redraw deletes it."""
    out = []
    for i, it in enumerate(items):
        if getattr(it, "media_type", None) != "playlist":
            continue
        image, _ = _thumb_of(it)
        if image is not None and getattr(image, "provider", None) == provider and not exists(getattr(image, "path", "")):
            out.append(i)
    return out


def with_thumb(item, new):
    """A copy of the item with its thumb replaced; MA's own object stays untouched."""
    old, where = _thumb_of(item)
    item = copy.copy(item)
    if where == "image":
        item.image = new
    else:
        meta = copy.copy(item.metadata)
        meta.images = type(meta.images)([new if img is old else img for img in meta.images])
        item.metadata = meta
    return item
