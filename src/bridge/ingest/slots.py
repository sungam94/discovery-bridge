"""Spec §3.2: sticky Daily Mix slots."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class Slot:
    name: str
    spotify_id: str | None
    last_seen: datetime | None
    stale: bool


def assign_daily_mixes(listed: list[str], current: list[Slot], now: datetime) -> tuple[list[Slot], list[str]]:
    slots = []
    for s in current:
        if s.spotify_id in listed:
            slots.append(replace(s, last_seen=now, stale=False))
        elif s.spotify_id is not None and s.last_seen is not None:
            slots.append(replace(s, stale=now - s.last_seen > STALE_AFTER))
        else:
            slots.append(s)
    held = {s.spotify_id for s in slots}
    unslotted: list[str] = []
    for pid in listed:
        if pid in held:
            continue
        free = [i for i, s in enumerate(slots) if s.spotify_id is None]
        if not free:
            free = sorted((i for i, s in enumerate(slots) if s.stale), key=lambda i: slots[i].last_seen)
        if not free:
            unslotted.append(pid)
            continue
        i = free[0]
        slots[i] = Slot(slots[i].name, pid, now, False)
        held.add(pid)
    return slots, unslotted
