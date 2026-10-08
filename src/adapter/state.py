"""Spotify Connect player_state (spike check 12): the track, pause state and active device of the web player.
Progress is read from the player bar (driver), not from these timestamps."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Our own web-player device reports its state with PUT .../track-playback/v1/devices/<id>/state; that <id> is the
# Connect active_device_id. Other URLs carry old devices (DELETE at page load) or session hashes (hobs_...).
_OWN_STATE = re.compile(r"/track-playback/v1/devices/([0-9a-f]{16,})/state")
# Our page also transfers playback to its newly registered device after every navigation.
_OWN_TRANSFER = re.compile(r"/connect-state/v1/connect/transfer/from/[0-9a-f]+/to/([0-9a-f]{16,})")


@dataclass(frozen=True)
class PlayerState:
    track_id: str | None
    linked_from_id: str | None
    is_paused: bool
    is_playing: bool
    ts_ms: int
    duration_ms: int | None
    context_uri: str | None
    is_ad: bool
    active_device_id: str | None


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _track_id(uri) -> str | None:
    uri = str(uri or "")
    return uri.rsplit(":", 1)[1] if uri.startswith("spotify:track:") else None


def parse_player_state(text: str) -> PlayerState | None:
    if "player_state" not in text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    ps = _find(data, "player_state")
    if not isinstance(ps, dict) or not isinstance(ps.get("track"), dict):
        return None
    track = ps["track"]
    uri = str(track.get("uri") or "")
    linked = track.get("linked_from_uri") or (track.get("metadata") or {}).get("linked_from_uri")
    active = _find(data, "active_device_id")
    return PlayerState(
        track_id=_track_id(uri), linked_from_id=_track_id(linked),
        is_paused=bool(ps.get("is_paused")), is_playing=bool(ps.get("is_playing")),
        ts_ms=_int(ps.get("timestamp")), duration_ms=_int(ps.get("duration")) or None,
        context_uri=ps.get("context_uri"), is_ad=uri.startswith("spotify:ad:"),
        active_device_id=str(active) if active else None)


class StateTracker:
    """Latest accepted state with a sequence number; an older timestamp never replaces a newer state."""

    def __init__(self) -> None:
        self._seq, self._state = 0, None

    def offer(self, text: str) -> bool:
        st = parse_player_state(text)
        if st is None or (self._state is not None and st.ts_ms < self._state.ts_ms):
            return False
        self._seq, self._state = self._seq + 1, st
        return True

    def current(self) -> tuple[int, PlayerState | None]:
        return self._seq, self._state


def own_device_from_request(method: str, url: str) -> str | None:
    if method == "PUT":
        m = _OWN_STATE.search(url)
    elif method == "POST":
        m = _OWN_TRANSFER.search(url)
    else:
        return None
    return m.group(1) if m else None


def load_settled(goto_t: float, last_command_t: float | None, last_state_t: float | None) -> bool:
    """A page load has settled when every Connect command the page sent after the goto has a state after it.
    The page's own resume-on-load sends a play command; deciding (and clicking) before its state arrives races it."""
    if last_command_t is None or last_command_t < goto_t:
        return True
    return last_state_t is not None and last_state_t > last_command_t
