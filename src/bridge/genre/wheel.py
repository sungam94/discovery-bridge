"""The genre colour wheel: every genre gets a hue (0-360) so that neighbouring hues sound alike.

The wheel runs from black metal (0) through the guitar genres to pop, on through folk, soul, jazz and hip hop
to downtempo and ambient, then through the electronic genres up to psycore, which closes the circle next to
black metal. A genre gets its hue from its name; one whose name says nothing is placed between the genres it
shares artists with, and then keeps that place."""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from datetime import datetime

MIN_EVIDENCE = 3  # shared-artist links needed before a genre is placed from the data
MIN_AGREEMENT = 0.6  # how closely those links must agree (1 = all on one hue); spread links say nothing
# name -> hue; matched against the end of a genre name, longest first (hyphens count as spaces)
ANCHORS: dict[str, float] = {
    "black metal": 0, "atmospheric black metal": 352, "blackgaze": 350,
    "death metal": 12, "deathcore": 13, "grindcore": 14, "thrash metal": 14, "metalcore": 15, "hardcore": 16,
    "mathcore": 17, "metal": 18, "post hardcore": 18, "progressive metal": 20, "alternative metal": 21,
    "nu metal": 21, "heavy metal": 22, "drone metal": 26, "doom metal": 28, "doom": 28, "sludge metal": 30,
    "sludge": 30, "post metal": 36, "doomgaze": 40, "stoner metal": 44, "stoner rock": 46, "stoner": 46,
    "desert rock": 47, "heavy psych": 49,
    "hard rock": 56, "classic rock": 58, "blues rock": 60, "blues": 61, "rock": 62,
    "psychedelic rock": 70, "space rock": 72, "krautrock": 73, "progressive rock": 74, "art rock": 76,
    "neo psychedelia": 78, "post rock": 82, "math rock": 85, "shoegaze": 92, "dream pop": 95, "slowcore": 97,
    "post punk": 102, "emo": 103, "grunge": 104, "indie rock": 105, "alternative rock": 105, "garage rock": 106,
    "punk": 107, "psychedelic pop": 110, "pop rock": 112, "indie pop": 116, "pop": 118, "new wave": 120,
    "synth pop": 122, "dance pop": 123, "electropop": 124,
    "folk rock": 130, "indie folk": 132, "folk": 135, "singer songwriter": 138, "country": 140, "americana": 141,
    "r&b": 148, "soul": 150, "funk": 152, "disco": 154, "jazz": 162, "jazz fusion": 164, "fusion": 164,
    "hip hop": 178, "rap": 178, "trap": 180,
    "trip hop": 190, "lo fi": 191, "downtempo": 193, "chillout": 195, "dub": 197, "reggae": 199, "ska": 200,
    "dancehall": 201, "ambient": 208, "drone": 210, "new age": 212, "classical": 214, "neoclassical": 214,
    "psybient": 220, "psychill": 221,
    "indietronica": 232, "electronica": 235, "electronic": 235, "idm": 237, "glitch": 239,
    "electro": 248, "house": 250, "edm": 251, "dance": 252, "techno": 253,
    "uk garage": 264, "breakbeat": 266, "drum and bass": 268, "jungle": 269, "bass": 270, "dubstep": 271,
    "future bass": 272,
    "trance": 285, "progressive trance": 288, "progressive psytrance": 300, "psytrance": 310, "psy trance": 310,
    "goa trance": 312, "goa": 312, "full on": 314, "forest": 325, "darkpsy": 327, "hi tech": 338, "psycore": 340,
    # harsh electronic sounds sit on the seam between psycore and black metal
    "industrial": 345, "electro industrial": 344, "ebm": 343, "noise": 348,
    "chillwave": 192, "glitch hop": 241, "surf": 106,
}
_BY_LENGTH = sorted(ANCHORS, key=len, reverse=True)


def _norm(genre: str) -> str:
    return " ".join(genre.casefold().replace("-", " ").split())


def anchor_hue(genre: str) -> float | None:
    """The hue the genre's name gives it: the longest wheel name its name ends with."""
    g = _norm(genre)
    for name in _BY_LENGTH:
        if g == name or g.endswith(" " + name):
            return ANCHORS[name]
    return None


def _circular_mean(hues: list[float]) -> float | None:
    """Mean hue, or None when the hues spread around the wheel (their mean would be an arbitrary point)."""
    x = sum(math.cos(math.radians(h)) for h in hues) / len(hues)
    y = sum(math.sin(math.radians(h)) for h in hues) / len(hues)
    if math.hypot(x, y) < MIN_AGREEMENT:
        return None
    return round(math.degrees(math.atan2(y, x)) % 360, 1)


def _from_data(conn: sqlite3.Connection, genre: str) -> float | None:
    """Between the named genres that the genre shares artists with; None while there is too little evidence."""
    key, links = _norm(genre), []
    for row in conn.execute("SELECT genres FROM artist_genre WHERE genres != '[]'"):
        names = [g for g, _ in json.loads(row["genres"])]
        if any(_norm(n) == key for n in names):
            links += [h for n in names if _norm(n) != key and (h := anchor_hue(n)) is not None]
    return _circular_mean(links) if len(links) >= MIN_EVIDENCE else None


def hues_for(conn: sqlite3.Connection, genres: Iterable[str], overrides: dict[str, float],
             now: datetime) -> dict[str, float | None]:
    """Hue per genre: configured override, else the name's place, else the stored or newly found data place."""
    over = {_norm(k): float(v) for k, v in (overrides or {}).items()}
    out: dict[str, float | None] = {}
    for genre in genres:
        key = _norm(genre)
        hue = over.get(key)
        if hue is None:
            hue = anchor_hue(genre)
        if hue is None:
            row = conn.execute("SELECT hue FROM genre_hue WHERE genre = ?", (key,)).fetchone()
            hue = row["hue"] if row else _from_data(conn, genre)
            if hue is not None and row is None:
                conn.execute("INSERT INTO genre_hue(genre, hue, placed_at) VALUES (?, ?, ?)",
                             (key, hue, now.isoformat()))
        out[genre] = hue
    return out
