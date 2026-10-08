"""The bridge writes layout.json and the MA plugin reads it: both sides must agree on its keys and the cover style."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from analysis.store import open_store, save
from bridge.config import DEFAULT_COVER_STYLE
from bridge.publish.layout import write_layout
from tests.analysis.helpers import bridge_db, playlist

_spec = importlib.util.spec_from_file_location(
    "spotify_bridge_frames", Path(__file__).parents[1] / "ma_provider" / "spotify_bridge" / "frames.py")
frames = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frames)

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def test_bridge_and_plugin_share_the_default_cover_style():
    assert frames.DEFAULT_STYLE == DEFAULT_COVER_STYLE


def test_plugin_finds_the_bridge_moods_under_the_playlist_name(tmp_path):
    conn = bridge_db(tmp_path / "bridge.sqlite")
    playlist(conn, "discover_weekly", "Spotify · Discover Weekly", [f"I{n}" for n in range(5)])
    sound = open_store(tmp_path / "sound.sqlite")
    for n in range(5):
        save(sound, f"I{n}", "done", NOW, moods={"space": 0.5, "energetic": 0.4}, instrumental=0.8,
             embedding=[0.0])
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, DEFAULT_COVER_STYLE, sound_path=tmp_path / "sound.sqlite", language="de")
    layout = json.loads((out / "layout.json").read_text())
    # the plugin looks the entry up the same way it looks up the genres: by the MA playlist name
    mood = (layout.get("moods") or {}).get("Spotify · Discover Weekly")
    assert frames.footer_text(mood) == "SPACE · ENERGETIC · INSTRUMENTAL"
