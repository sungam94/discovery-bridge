"""Operator commands. `publish-once <slot>` ingests and publishes one fixed slot (live checkpoint)."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx

from bridge.config import load_config
from bridge.db import connect, migrate
from bridge.ingest.ingestor import Ingestor
from bridge.ma.client import MaClient
from bridge.publish.publisher import Publisher, ensure_playlist_row, playlist_name
from bridge.resolve.forward import ForwardResolver
from bridge.spotify.client import SpotifyClient
from bridge.spotify.client import SpotifyError
from bridge.spotify.parse import fixed_playlist_ids
from bridge.spotify.session import make_session
from bridge.timeutil import utcnow


def load_dotenv(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def fixed_slot_id(cfg, conn, spotify, slot: str) -> str | None:
    """Same order as the poller: config.yaml, the user's hub, the ID stored by an earlier poll."""
    configured = getattr(cfg, f"{slot}_id")
    if configured:
        return configured
    try:
        found = fixed_playlist_ids(await spotify.made_for_you()).get(slot)
    except SpotifyError as exc:
        print("hub not readable:", exc)
        found = None
    if found:
        return found
    row = conn.execute("SELECT spotify_id FROM playlist WHERE slot = ?", (slot,)).fetchone()
    return row["spotify_id"] if row else None


async def publish_once(cfg, slot: str) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db_path)
    migrate(conn)
    ma = MaClient(cfg.ma_url, cfg.ma_token)
    try:
        async with httpx.AsyncClient() as http:
            spotify = SpotifyClient(make_session(cfg, conn), http)
            spotify_id = await fixed_slot_id(cfg, conn, spotify, slot)
            if spotify_id is None:
                print(f"{slot}: not found in your hub; set {slot}_id in config.yaml")
                return
            ensure_playlist_row(conn, slot, "fixed", spotify_id)
            ing = Ingestor(conn, spotify, ForwardResolver(conn, ma, utcnow), utcnow, cfg.request_delay_s)
            print("ingest:", await ing.ingest_slot(slot, spotify_id))
            print("publish:", await Publisher(conn, ma, utcnow, publish_history=cfg.publish_history,
                                                tz=cfg.timezone).check(slot))
        print("MA playlist found by name:", await ma.find_playlist_by_name(playlist_name(slot)) is not None)
    finally:
        await ma.close()
    snap = conn.execute("SELECT coverage_matched, coverage_total FROM playlist_snapshot WHERE slot=? "
                        "ORDER BY id DESC LIMIT 1", (slot,)).fetchone()
    if snap is None:
        print("coverage: no snapshot (empty or held fetch)")
        return
    print(f"coverage: {snap['coverage_matched']}/{snap['coverage_total']}")
    for r in conn.execute("SELECT s.artist, s.title, m.unmatched_reason FROM track_mapping m JOIN source_track s "
                          "ON s.provider_item_id = m.spotify_id WHERE m.status = 'unmatched'"):
        print("  unmatched:", r["artist"].replace("\x1f", ", "), "-", r["title"], f"({r['unmatched_reason']})")


def main() -> None:
    p = argparse.ArgumentParser(prog="bridge.cli")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    po = sub.add_parser("publish-once")
    po.add_argument("slot", choices=["discover_weekly", "release_radar"])
    args = p.parse_args()
    load_dotenv(Path(".env"))
    cfg = load_config(Path(args.config), os.environ)
    if args.cmd == "publish-once":
        asyncio.run(publish_once(cfg, args.slot))


if __name__ == "__main__":
    main()
