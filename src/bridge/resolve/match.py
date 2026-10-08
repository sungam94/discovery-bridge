"""Spec §4.2: duration gate and tie-break. MA durations are seconds; Spotify durations are milliseconds."""
from __future__ import annotations

from bridge.models import MaCandidate, SpotifyCandidate, SpotifyTrack


def passes_gate(source_ms: int, candidate_s: int) -> bool:
    tolerance_s = max(5.0, 0.03 * source_ms / 1000)
    return abs(source_ms / 1000 - candidate_s) <= tolerance_s


def choose(track: SpotifyTrack, candidates: list[MaCandidate]) -> MaCandidate | None:
    cands = [c for c in candidates if passes_gate(track.duration_ms, c.duration_s)]
    if not cands:
        return None
    filters = [
        lambda c: abs(track.duration_ms / 1000 - c.duration_s) <= 2,
        lambda c: c.explicit is None or c.explicit == track.explicit,
        lambda c: c.album.casefold() == track.album.casefold(),
        lambda c: not c.compilation,
    ]
    for f in filters:
        narrowed = [c for c in cands if f(c)]
        cands = narrowed or cands
    return min(cands, key=lambda c: int(c.item_id))


def candidates_from_search(results: list[dict]) -> list[MaCandidate]:
    out: list[MaCandidate] = []
    for t in results:
        isrcs = frozenset(str(v).upper() for k, v in (t.get("external_ids") or []) if k == "isrc")
        provider = str(t.get("provider", ""))
        if provider.startswith("tidal"):
            uri, item_id, from_library = t["uri"], str(t["item_id"]), False
        elif provider == "library":
            pm = next((p for p in t.get("provider_mappings", []) if p.get("provider_domain") == "tidal"), None)
            if pm is None:
                continue
            uri, item_id, from_library = f"{pm['provider_instance']}://track/{pm['item_id']}", str(pm["item_id"]), True
        else:
            continue
        album = t.get("album") or {}
        out.append(MaCandidate(
            uri=uri, item_id=item_id, isrcs=isrcs, duration_s=int(t.get("duration") or 0),
            explicit=(t.get("metadata") or {}).get("explicit"), album=album.get("name", ""),
            compilation=(album.get("album_type") == "compilation") if "album_type" in album else None,
            from_library=from_library,
        ))
    return out


def choose_spotify(duration_s: int, explicit: bool | None, album: str,
                   cands: list[SpotifyCandidate]) -> SpotifyCandidate | None:
    """Spec §4.2 for MA item -> Spotify. searchTracks has no release date or compilation flag, so the
    last tie-break is the smallest ID (ruling in the Phase 2 plan)."""
    src_ms = duration_s * 1000
    tolerance_ms = max(5000.0, 0.03 * src_ms)
    cands = [c for c in cands if abs(c.duration_ms - src_ms) <= tolerance_ms]
    if not cands:
        return None
    filters = [
        lambda c: abs(c.duration_ms - src_ms) <= 2000,
        lambda c: explicit is None or c.explicit == explicit,
        lambda c: c.album.casefold() == album.casefold(),
    ]
    for f in filters:
        narrowed = [c for c in cands if f(c)]
        cands = narrowed or cands
    return min(cands, key=lambda c: c.id)
