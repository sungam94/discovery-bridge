"""Spec §3.3 step 2: bad-fetch guard."""
from __future__ import annotations

from dataclasses import dataclass, replace

from bridge.hashing import ordered_hash


@dataclass(frozen=True)
class GuardState:
    guard_count: int = 0
    guard_hash: str | None = None
    empty_count: int = 0


def check_fetch(ids: list[str], prev_len: int | None, state: GuardState) -> tuple[bool, GuardState]:
    if not ids:
        return False, replace(state, empty_count=state.empty_count + 1)
    if prev_len and len(ids) < prev_len / 2:
        h = ordered_hash(ids)
        count = state.guard_count + 1 if state.guard_hash == h else 1
        if count >= 3:
            return True, GuardState()
        return False, GuardState(guard_count=count, guard_hash=h, empty_count=0)
    return True, GuardState()
