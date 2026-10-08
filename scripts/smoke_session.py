"""Manual smoke test: capture Spotify credentials and print non-secret facts.

Usage: uv run python scripts/smoke_session.py [probe playlist ID]
The probe playlist only teaches the session the fetchPlaylist hash; any playlist works. Without an argument it
is a public editorial playlist.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

from bridge.spotify.session import PUBLIC_PROBE_PLAYLIST, HeadlessSession


def _load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


async def main(probe: str) -> None:
    _load_env(Path(".env"))
    c = await HeadlessSession(os.environ["SPOTIFY_SP_DC"], probe, locale="en-US",
                              timezone=os.environ.get("TZ") or "UTC").capture()
    print({"expires_in_s": round(c.expires_at - time.time()), "app_version": c.app_version,
           "ops": sorted(c.hashes), "user_agent": c.user_agent, "token_len": len(c.token)})


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else PUBLIC_PROBE_PLAYLIST))
