"""Entry point of the sound-analysis container: python -m analysis.main"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

import httpx

from analysis.deezer import Deezer
from analysis.model import EssentiaAnalyser
from analysis.service import Service
from analysis.store import open_store

log = logging.getLogger("analysis")


def settings(env: Mapping[str, str]) -> tuple[str, str, str]:
    return (env.get("BRIDGE_DB", "/data/bridge.sqlite"), env.get("SOUND_DB", "/data/sound.sqlite"),
            env.get("MODELS_DIR", "/models"))


def open_bridge(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would show the signed clip URLs
    bridge_path, sound_path, models = settings(os.environ)
    sound = open_store(sound_path)  # first, so a container that fails on the models still shows in the health file
    analyser = EssentiaAnalyser(Path(models))
    http = httpx.Client(headers={"User-Agent": "discovery-bridge-sound/1"})
    with tempfile.TemporaryDirectory() as work:
        log.info("sound analysis started")
        Service(open_bridge(bridge_path), sound, Deezer(http), analyser, Path(work)).run()


if __name__ == "__main__":
    main()
