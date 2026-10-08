from bridge.hashing import items_hash, ordered_hash, set_hash


def test_ordered_vs_set():
    assert ordered_hash(["a", "b"]) != ordered_hash(["b", "a"])
    assert set_hash(["a", "b"]) == set_hash(["b", "a", "a"])
    assert items_hash(["u2", "u1"]) == items_hash(["u1", "u2"])
    assert len(ordered_hash([])) == 64
