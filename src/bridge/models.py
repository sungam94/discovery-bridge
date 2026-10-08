from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpotifyTrack:
    id: str
    name: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int
    explicit: bool
    isrc: str | None = None


@dataclass(frozen=True)
class MaCandidate:
    uri: str              # TIDAL provider URI, e.g. "tidal--test0001://track/900026000"
    item_id: str          # TIDAL numeric ID as string
    isrcs: frozenset[str]
    duration_s: int
    explicit: bool | None
    album: str
    compilation: bool | None
    from_library: bool


@dataclass(frozen=True)
class FetchedPlaylist:
    tracks: list[SpotifyTrack]
    revision_id: str | None


@dataclass(frozen=True)
class SpotifyCandidate:
    id: str            # base62 Spotify track ID
    name: str
    duration_ms: int
    explicit: bool
    album: str
