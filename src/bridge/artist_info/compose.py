"""Text for a Music Assistant artist page: a bio, then short facts, then where the bio came from."""
from __future__ import annotations

import html
import re

MAX_LIST = 5
MAX_MEMBERS = 6
_LASTFM_TAIL = re.compile(r"\s*<a [^>]*>Read more on Last\.fm</a>.*$", re.S)


def clean_lastfm_bio(raw: str | None) -> str:
    """Last.fm bios end with a 'Read more' link and a licence note; both go, and so does any other HTML."""
    if not raw:
        return ""
    text = _LASTFM_TAIL.sub("", raw)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip().rstrip(".").strip() + "." if text.strip() else ""


def _listing(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} and {len(items) - limit} more" if len(items) > limit else shown


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def compose(bio: str, bio_source: str | None, mb: dict | None, lastfm: dict | None, local: dict) -> str | None:
    facts: list[str] = []
    if mb:
        verb = "Born" if mb.get("type") == "Person" else "Formed"
        start = " ".join(p for p in (mb.get("begin"), f"in {mb['area']}" if mb.get("area") else None) if p)
        if start:
            facts.append(f"{verb} {start}")
        if mb.get("members"):
            facts.append(f"Members: {_listing(mb['members'], MAX_MEMBERS)}")
    if lastfm:
        n = lastfm.get("listeners")
        parts = [f"{n:,} listener{'' if n == 1 else 's'}"] if n else []
        if lastfm.get("tags"):
            parts.append(", ".join(lastfm["tags"][:3]))
        if parts:
            facts.append("Last.fm: " + " · ".join(parts))
        if lastfm.get("similar"):
            facts.append(f"Similar: {_listing(lastfm['similar'], MAX_LIST)}")
    mine = []
    if local.get("playlists"):
        mine.append(f"In your Spotify mixes: {_listing(local['playlists'], MAX_LIST)}")
    if local.get("plays"):
        mine.append(f"played {_times(local['plays'])} in Music Assistant")
    if mine:
        facts.append(" · ".join(mine))
    blocks = [b for b in (bio.strip(), "\n".join(facts)) if b]
    if not blocks:
        return None
    if bio.strip() and bio_source:
        blocks.append(f"Bio: {bio_source} (CC BY-SA)")
    return "\n\n".join(blocks)
