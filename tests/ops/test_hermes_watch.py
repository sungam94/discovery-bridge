import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "discovery_bridge_watch", Path(__file__).parents[2] / "ops" / "hermes" / "discovery_bridge.py")
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def health(*problems, age_min=3, at=NOW):
    return {"version": 1, "written_at": (at - timedelta(minutes=age_min)).isoformat(),
            "problems": [{"key": k, "text": t} for k, t in problems]}


def test_silent_when_healthy():
    assert watch.check(health(), NOW, {}) == ("", {})


def test_new_problem_then_silence_then_recovery():
    msg, st = watch.check(health(("spotify", "Spotify: auth_expired")), NOW, {})
    assert "⚠" in msg and "auth_expired" in msg
    later = NOW + timedelta(minutes=15)
    msg2, st = watch.check(health(("spotify", "Spotify: auth_expired"), at=later), later, st)
    assert msg2 == ""
    later = NOW + timedelta(minutes=30)
    msg3, st = watch.check(health(at=later), later, st)
    assert "✅" in msg3 and "auth_expired" in msg3 and st == {}


def test_reminder_after_a_day():
    _, st = watch.check(health(("disk", "low disk")), NOW, {})
    later = NOW + timedelta(hours=25)
    msg, _ = watch.check(health(("disk", "low disk"), at=later), later, st)
    assert "low disk" in msg


def test_stale_or_missing_file_is_a_problem():
    msg, st = watch.check(health(age_min=45), NOW, {})
    assert "⚠" in msg and "bridge" in st
    msg, _ = watch.check(None, NOW, {})
    assert "⚠" in msg


def test_frame_texts_are_german():
    msg, st = watch.check(health(("disk", "low disk")), NOW, {})
    assert msg == "Discovery Bridge\n⚠ low disk"
    later = NOW + timedelta(hours=25)
    msg, st = watch.check(health(("disk", "low disk"), at=later), later, st)
    assert msg == "Discovery Bridge\n⚠ weiterhin: low disk"
    msg, _ = watch.check(health(at=later), later, st)
    assert msg == "Discovery Bridge\n✅ wieder ok: low disk"
    msg, _ = watch.check(None, NOW, {})
    assert msg == "Discovery Bridge\n⚠ health.json fehlt oder ist unlesbar"
    msg, _ = watch.check(health(age_min=45), NOW, {})
    assert msg.startswith("Discovery Bridge\n⚠ keine Statusmeldung seit ")


def test_frame_texts_in_english():
    msg, st = watch.check(health(("disk", "low disk")), NOW, {}, language="en")
    assert msg == "Discovery Bridge\n⚠ low disk"
    later = NOW + timedelta(hours=25)
    msg, st = watch.check(health(("disk", "low disk"), at=later), later, st, language="en")
    assert msg == "Discovery Bridge\n⚠ still: low disk"
    msg, _ = watch.check(health(at=later), later, st, language="en")
    assert msg == "Discovery Bridge\n✅ ok again: low disk"
    msg, _ = watch.check(None, NOW, {}, language="en")
    assert msg == "Discovery Bridge\n⚠ health.json is missing or unreadable"
    msg, _ = watch.check(health(age_min=45), NOW, {}, language="en")
    assert msg.startswith("Discovery Bridge\n⚠ no status update since ")
