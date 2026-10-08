"""adapter/start.sh runs again after a crash inside the same container (Docker restart keeps /tmp), so stale
X locks must be cleared, and a display that never comes up must end the container instead of hanging it."""
from pathlib import Path

SCRIPT = (Path(__file__).parents[2] / "adapter" / "start.sh").read_text()


def test_stale_x_display_lock_is_removed_before_xvfb_starts():
    clear = SCRIPT.index("/tmp/.X99-lock")
    assert "/tmp/.X11-unix/X99" in SCRIPT[:SCRIPT.index("Xvfb :99")]
    assert clear < SCRIPT.index("Xvfb :99")


def test_waiting_for_the_display_is_bounded_and_fails_the_container():
    wait = SCRIPT[SCRIPT.index("Xvfb :99"):SCRIPT.index("pulseaudio")]
    assert "until xdpyinfo" not in wait          # an unbounded wait hung the adapter for hours (2026-10-04)
    assert "exit 1" in wait
