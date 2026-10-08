import os
import sqlite3
import time

import pytest

import analysis.main
from analysis.main import open_bridge, settings
from analysis.store import open_store
from tests.analysis.helpers import bridge_db, playlist


def test_bridge_database_is_opened_read_only(tmp_path):
    playlist(bridge_db(tmp_path / "bridge.sqlite"), "a", "Spotify · A", ["I1"])
    conn = open_bridge(tmp_path / "bridge.sqlite")
    assert conn.execute("SELECT count(*) FROM playlist").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM playlist")


def test_settings_from_environment():
    assert settings({}) == ("/data/bridge.sqlite", "/data/sound.sqlite", "/models")
    assert settings({"BRIDGE_DB": "/x/b", "SOUND_DB": "/x/s", "MODELS_DIR": "/m"}) == ("/x/b", "/x/s", "/m")


def test_sound_database_exists_before_the_models_load(tmp_path, monkeypatch):
    # a container that dies loading the models still leaves sound.sqlite, so the health check can report it
    playlist(bridge_db(tmp_path / "bridge.sqlite"), "a", "Spotify · A", ["I1"])
    monkeypatch.setenv("BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    monkeypatch.setenv("SOUND_DB", str(tmp_path / "sound.sqlite"))

    def broken(models):
        raise RuntimeError("model graph missing")

    monkeypatch.setattr(analysis.main, "EssentiaAnalyser", broken)
    with pytest.raises(RuntimeError):
        analysis.main.main()
    conn = sqlite3.connect(tmp_path / "sound.sqlite")
    assert conn.execute("SELECT count(*) FROM track_sound").fetchone()[0] == 0


def test_reopening_the_store_keeps_its_modification_time(tmp_path):
    # the stall alert dates an empty sound.sqlite by its mtime, so a restart loop must not refresh it
    open_store(tmp_path / "sound.sqlite").close()
    old = time.time() - 7 * 3600
    os.utime(tmp_path / "sound.sqlite", (old, old))
    open_store(tmp_path / "sound.sqlite").close()
    assert os.stat(tmp_path / "sound.sqlite").st_mtime == pytest.approx(old)
