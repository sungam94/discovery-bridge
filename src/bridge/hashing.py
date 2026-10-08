from __future__ import annotations

import hashlib
from collections.abc import Sequence


def _h(parts: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def ordered_hash(ids: Sequence[str]) -> str:
    return _h(list(ids))


def set_hash(ids: Sequence[str]) -> str:
    return _h(sorted(set(ids)))


def items_hash(uris: Sequence[str]) -> str:
    return _h(sorted(set(uris)))
