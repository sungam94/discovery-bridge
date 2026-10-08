"""Our covers for TIDAL and SoundCloud playlists on Discover: which cards get one, the image name our provider serves
it under, and the cover files kept under MA's storage path. Pure; no Music Assistant imports.

MA caches every image by (provider, path) with no expiry for paths that are not URLs, so the name must change
whenever the drawing would: it hashes the layout entry, the cover style, the hues of the entry's genres, the
playlist's own artwork and RENDER_VERSION (bump it when frames.py draws differently)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

RENDER_VERSION = 2
PREFIX = "sbcover"
NAME_RE = re.compile(rf"^{PREFIX}_[0-9a-f]{{16}}_[0-9a-f]{{16}}\.jpg$")
INDEX = "index.json"   # name -> what is needed to draw it, for requests that arrive before Discover is listed again
INDEX_MAX = 1000
FILE_MAX_AGE_S = 14 * 24 * 3600   # a cover file not drawn for this long is removed; its index record redraws it


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def uri_key(uri: str) -> str:
    return _hash(uri)


def cover_name(uri: str, entry: dict, style, hues: dict, art_path: str, art_provider: str) -> str:
    used = _used_hues(entry, hues)
    content = json.dumps({"v": RENDER_VERSION, "entry": entry, "style": style, "hues": used,
                          "art": [art_path, art_provider]}, sort_keys=True, default=str)
    return f"{PREFIX}_{uri_key(uri)}_{_hash(content)}.jpg"


def valid_name(name: str) -> bool:
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None


def _used_hues(entry: dict, hues) -> dict:
    hues = hues if isinstance(hues, dict) else {}
    names = [g if isinstance(g, str) else g[0] for g in (entry.get("genres") or [])]
    return {g: hues[g] for g in names if g in hues}


def _is_thumb(img) -> bool:
    kind = getattr(img, "type", None)
    return getattr(kind, "value", kind) == "thumb"


def with_source_covers(items: list, layout, make_image: Callable[[str], object]) -> tuple[list, dict]:
    """Copies of the TIDAL/SoundCloud playlist cards named in layout["sources"] whose thumb is make_image(name);
    MA's own objects stay untouched. Also returns {name: record} for each cover, record holding the playlist's
    own artwork and its layout entry so the cover can be drawn when MA asks for it."""
    found = layout.get("sources") if isinstance(layout, dict) else None
    if not isinstance(found, dict) or not found:
        return items, {}
    out, seen = [], {}
    for it in items:
        entry = found.get(getattr(it, "uri", None))
        if getattr(it, "media_type", None) != "playlist" or not isinstance(entry, dict):
            out.append(it)
            continue
        meta = getattr(it, "metadata", None)
        images = list(getattr(meta, "images", None) or [])
        thumb = next((img for img in images if _is_thumb(img)), None)
        mapped = thumb is None and _is_thumb(getattr(it, "image", None))   # an ItemMapping carries one image
        if mapped:
            thumb = it.image
        if thumb is None or valid_name(getattr(thumb, "path", "")):
            out.append(it)
            continue
        name = cover_name(it.uri, entry, layout.get("cover_style"), layout.get("hues"), thumb.path, thumb.provider)
        seen[name] = {"uri": it.uri, "path": thumb.path, "provider": thumb.provider, "entry": entry,
                      "style": layout.get("cover_style"), "hues": _used_hues(entry, layout.get("hues"))}
        it = copy.copy(it)
        if mapped:
            it.image = make_image(name)
        else:
            meta = copy.copy(meta)
            meta.images = type(meta.images)([make_image(name) if img is thumb else img for img in images])
            it.metadata = meta
        out.append(it)
    return out, seen


def _write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cached(folder: Path, name: str) -> bytes | None:
    if not valid_name(name):
        return None
    try:
        return (Path(folder) / name).read_bytes()
    except OSError:
        return None


def store(folder: Path, name: str, data: bytes) -> None:
    """Write the cover atomically and remove the playlist's older covers."""
    folder = Path(folder)
    if not valid_name(name):
        raise ValueError(f"invalid cover name {name!r}")
    _write(folder / name, data)
    same = f"{PREFIX}_{name.split('_')[1]}_"
    for p in folder.glob(f"{same}*.jpg"):
        if p.name != name:
            p.unlink(missing_ok=True)


def _load_index(folder: Path) -> dict:
    try:
        data = json.loads((Path(folder) / INDEX).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(folder: Path, seen: dict) -> None:
    """Add the records to the index; a playlist's older names are dropped. Cover files whose name left the index,
    or that were not drawn for FILE_MAX_AGE_S, are removed, so playlists that left Discover do not pile up."""
    index = _load_index(folder)
    keys = {n.split("_")[1] for n in seen}
    index = {n: r for n, r in index.items() if valid_name(n) and n.split("_")[1] not in keys}
    index.update(seen)
    index = dict(list(index.items())[-INDEX_MAX:])
    _write(Path(folder) / INDEX, json.dumps(index, default=str).encode())
    _prune(Path(folder), index)


def _prune(folder: Path, index: dict) -> None:
    old = time.time() - FILE_MAX_AGE_S
    for p in folder.glob(f"{PREFIX}_*.jpg"):
        try:
            if valid_name(p.name) and (p.name not in index or p.stat().st_mtime < old):
                p.unlink(missing_ok=True)
        except OSError:
            continue


def recall(folder: Path, name: str) -> dict | None:
    if not valid_name(name):
        return None
    record = _load_index(folder).get(name)
    return record if isinstance(record, dict) else None
