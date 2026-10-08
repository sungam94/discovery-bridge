from datetime import datetime, timedelta, timezone

from bridge.ingest.slots import Slot, assign_daily_mixes

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def empty():
    return [Slot(f"daily_mix_{i}", None, None, False) for i in range(1, 7)]


def test_first_assignment_in_order():
    slots, unslotted = assign_daily_mixes(["a", "b"], empty(), NOW)
    assert [s.spotify_id for s in slots] == ["a", "b", None, None, None, None]
    assert unslotted == []


def test_reorder_keeps_slots():
    slots, _ = assign_daily_mixes(["a", "b"], empty(), NOW)
    slots, _ = assign_daily_mixes(["b", "a"], slots, NOW)
    assert [s.spotify_id for s in slots[:2]] == ["a", "b"]


def test_missing_becomes_stale_after_7_days_and_is_reused():
    slots, _ = assign_daily_mixes(["a", "b", "c", "d", "e", "f"], empty(), NOW)
    slots, unslotted = assign_daily_mixes(["b", "c", "d", "e", "f", "g"], slots, NOW + timedelta(days=8))
    assert slots[0].spotify_id == "g" and slots[0].stale is False
    assert unslotted == []


def test_missing_less_than_7_days_keeps_slot_and_unslots_newcomer():
    slots, _ = assign_daily_mixes(["a", "b", "c", "d", "e", "f"], empty(), NOW)
    slots, unslotted = assign_daily_mixes(["b", "c", "d", "e", "f", "g"], slots, NOW + timedelta(days=2))
    assert slots[0].spotify_id == "a" and unslotted == ["g"]


def test_returning_id_clears_stale():
    slots = [Slot("daily_mix_1", "a", NOW - timedelta(days=9), True)] + empty()[1:]
    slots, _ = assign_daily_mixes(["a"], slots, NOW)
    assert slots[0].stale is False and slots[0].last_seen == NOW
