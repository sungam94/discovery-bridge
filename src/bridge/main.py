"""Service entry: event- or interval-driven poll with a timeout, daily backup, HTTPS status page."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from bridge.backup import run_backup
from bridge.capture.favorites import FavoriteTracker
from bridge.capture.plays import PlayTracker
from bridge.capture.recorder import PlayerDirectory, Recorder
from bridge.capture.service import CaptureService
from bridge.capture.source import SourceResolver
from bridge.cli import load_dotenv
from bridge.config import Config, load_config
from bridge.artist_info.played import PlayedArtists
from bridge.artist_info.sources import LastFm, Wikipedia
from bridge.artist_info.worker import ArtistInfoWorker, write_artist_file
from bridge.db import connect, migrate
from bridge.genre.musicbrainz import MusicBrainz
from bridge.genre.worker import GenreWorker
from bridge.health import build_health, record_backup, record_capture, record_poll, write_health
from bridge.ingest.ingestor import Ingestor
from bridge.ingest.poller import Poller
from bridge.ma.client import MaClient
from bridge.ma.events import stream_events
from bridge.publish.layout import write_layout
from bridge.publish.moods import playlist_moods, source_moods
from bridge.publish.publisher import Publisher
from bridge.repo import get_setting, set_setting
from bridge.queue.client import AdapterClient
from bridge.queue.runner import QueueRunner
from bridge.resolve.forward import ForwardResolver
from bridge.resolve.reverse import ReverseResolver
from bridge.sources.collector import SourceCollector
from bridge.spotify.client import SpotifyClient
from bridge.spotify.fallback import FallbackSource
from bridge.spotify.session import make_session
from bridge.status.app import create_app
from bridge.timeutil import utcnow

log = logging.getLogger("bridge")


def next_backup_time(now: datetime, tz: str, hour: int = 3, minute: int = 30) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target.astimezone(now.tzinfo)


WILDCARD_BINDS = ("0.0.0.0", "::", "")


def status_hostname(cfg) -> str:
    """Name in the status page certificate. The bridge runs with host networking, so the container's hostname
    is the server's."""
    return cfg.status_hostname or socket.gethostname()


def ensure_cert(tls_dir: Path, bind: str, hostname: str) -> None:
    """Self-signed certificate for the status page, made once; an existing one is never replaced. The IP is
    named in it only when the page binds to one concrete address."""
    cert, key = tls_dir / "cert.pem", tls_dir / "key.pem"
    if cert.exists() and key.exists():
        return
    tls_dir.mkdir(parents=True, exist_ok=True)
    names = f"DNS:{hostname}.local" if bind in WILDCARD_BINDS else f"IP:{bind},DNS:{hostname}.local"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                    "-subj", f"/CN={hostname}", "-addext", f"subjectAltName={names}",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)


def startup_warnings(cfg) -> list[str]:
    out = []
    if cfg.backup_copy_target is None:
        out.append("backup_copy_target is not set: backups live only on the same disk as the database")
    if not cfg.players_allowlist:
        out.append("players_allowlist is empty: no plays count as taste events until it is set")
    if not getattr(cfg, "feedback_api_token", None):
        out.append("FEEDBACK_API_TOKEN is not set: the feedback queue does not run")
    return out


async def poll_loop(poller: Poller, interval_s: int, wake: asyncio.Event, conn=None) -> None:
    def record(ok: bool, detail: str = "") -> None:
        if conn is not None:
            record_poll(conn, utcnow(), ok, detail)

    while True:
        try:
            log.info("poll: %s", await asyncio.wait_for(poller.run_once(), timeout=interval_s))
            record(True)
        except TimeoutError:
            log.error("poll exceeded %ss and was cancelled", interval_s)
            record(False, f"timeout after {interval_s}s")
        except Exception as exc:
            log.exception("poll failed")
            record(False, f"{type(exc).__name__}: {exc}")
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval_s)
        except TimeoutError:
            pass


async def health_loop(conn, cfg: Config, every_s: int = 300) -> None:
    """Writes health.json for any watchdog (one example: ops/hermes/discovery_bridge.py)."""
    while True:
        try:
            free = shutil.disk_usage(cfg.data_dir).free
            write_health(cfg.health_file, build_health(conn, utcnow(), cfg.poll_interval_s, free,
                                                       sound_path=cfg.sound_db_path, language=cfg.language))
        except Exception:
            log.exception("health file write failed")
        await asyncio.sleep(every_s)


async def genre_loop(worker: GenreWorker, every_s: int = 60, batch: int = 20) -> None:
    """Covers show the playlist genres and moods; the genre lookups run slowly in the background (one request per
    second). Every pass also checks whether a cover needs redrawing, e.g. because its moods became known."""
    while True:
        fetched = 0
        try:
            fetched = await worker.run_once(batch)
        except Exception:
            log.exception("genre worker failed")
        # while artists are pending, go on at once (the client itself keeps to one request per second)
        await asyncio.sleep(1 if fetched else every_s)


async def artist_info_loop(worker: ArtistInfoWorker, every_s: int = 300, batch: int = 1, pause_s: int = 8) -> None:
    """Text for MA artist pages: library artists first, then the playlists' artists; sources are asked gently."""
    while True:
        fetched = 0
        try:
            fetched = await worker.run_once(batch)
        except Exception:
            log.exception("artist info worker failed")
        # a pause between artists leaves MusicBrainz room for MA's own lookups (it needs the MBID to ask us)
        await asyncio.sleep(pause_s if fetched else every_s)


SOURCE_EVERY_S = 3 * 3600
SOURCE_RETRY_S = 600  # MA takes a minute or two after its own restart


async def source_loop(collector: SourceCollector, every_s: float = SOURCE_EVERY_S,
                      retry_s: float = SOURCE_RETRY_S) -> None:
    """TIDAL and SoundCloud playlists of MA's Discover rows, read at start and every few hours; the genre worker
    then puts them into the layout file for their covers. A failed pass is tried again sooner."""
    while True:
        wait = every_s
        try:
            await collector.run_once()
        except Exception:
            log.exception("source playlists collection failed")
            wait = retry_s
        await asyncio.sleep(wait)


BACKUP_CATCH_UP = timedelta(hours=24)


def backup_overdue(last_backup_at: str | None, now: datetime) -> bool:
    """A restart around the backup time used to skip a whole day; a start catches up a missed backup."""
    return last_backup_at is None or now - datetime.fromisoformat(last_backup_at) > BACKUP_CATCH_UP


def _backup_all(cfg: Config, day, conn=None) -> None:
    try:
        path = run_backup(cfg.db_path, cfg.backup_dir, day, copy_target=cfg.backup_copy_target)
        log.info("backup written: %s", path)
        if cfg.sound_db_path.exists():  # sound analysis results: hours of CPU to rebuild
            log.info("backup written: %s", run_backup(cfg.sound_db_path, cfg.backup_dir, day,
                                                      copy_target=cfg.backup_copy_target, name="sound"))
        if conn is not None:
            record_backup(conn, utcnow(), True)
    except Exception as exc:
        log.exception("backup failed")
        if conn is not None:
            record_backup(conn, utcnow(), False, f"{type(exc).__name__}: {exc}")


async def played_artists_loop(worker: PlayedArtists, every_s: int = 600, limit: int = 3) -> None:
    """Artists of tracks heard in MA join the MA library (a few per pass: each add makes MA refresh metadata,
    which it does at most once per 30 s)."""
    while True:
        try:
            await worker.run_once(limit)
        except Exception:
            log.exception("played artists worker failed")
        await asyncio.sleep(every_s)


async def backup_loop(cfg: Config, conn=None) -> None:
    if conn is not None and backup_overdue(get_setting(conn, "last_backup_at"), utcnow()):
        _backup_all(cfg, utcnow().astimezone(ZoneInfo(cfg.timezone)).date(), conn)
    while True:
        when = next_backup_time(utcnow(), cfg.timezone)
        await asyncio.sleep((when - utcnow()).total_seconds())
        _backup_all(cfg, when.astimezone(ZoneInfo(cfg.timezone)).date(), conn)


async def run(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    ensure_cert(cfg.tls_dir, cfg.status_bind, status_hostname(cfg))
    conn = connect(cfg.db_path)
    log.info("migrations applied: %s", migrate(conn))
    warnings = startup_warnings(cfg)
    for w in warnings:
        log.warning(w)
    ma = MaClient(cfg.ma_url, cfg.ma_token)
    http = httpx.AsyncClient()
    adapter_client = AdapterClient(cfg.adapter_url, cfg.feedback_api_token, http) if cfg.feedback_api_token else None
    queue = (QueueRunner(conn, adapter_client, utcnow, cfg.timezone, cfg.max_jobs_per_day, cfg.job_attempts,
                         cfg.max_job_age_s, cfg.play_bucket_hours, cfg.max_play_age_s)
             if adapter_client is not None else None)
    def load_meta() -> dict | None:
        raw = get_setting(conn, "spotify_session_meta")
        return json.loads(raw) if raw else None

    source = FallbackSource(make_session(cfg, conn), adapter_client, load_meta,
                            lambda meta: set_setting(conn, "spotify_session_meta", json.dumps(meta)))
    spotify = SpotifyClient(source, http)
    publisher = Publisher(conn, ma, utcnow, publish_history=cfg.publish_history, tz=cfg.timezone)
    ingestor = Ingestor(conn, spotify, ForwardResolver(conn, ma, utcnow), utcnow, cfg.request_delay_s,
                        max_backoff_s=cfg.poll_interval_s)
    poller = Poller(conn, spotify, ingestor, publisher, ma, cfg, utcnow)
    capture_ma = MaClient(cfg.ma_url, cfg.ma_token)  # own connection: the poller closes its client after each poll
    recorder = Recorder(conn, capture_ma, PlayerDirectory(capture_ma, cfg.players_allowlist, utcnow),
                        ReverseResolver(conn, capture_ma, spotify, utcnow), cfg.thresholds, utcnow)
    capture = CaptureService(lambda: stream_events(cfg.ma_url, cfg.ma_token), capture_ma, PlayTracker(conn, utcnow),
                             SourceResolver(conn, capture_ma, utcnow), recorder, FavoriteTracker(conn, utcnow), utcnow,
                             on_state=lambda up: record_capture(conn, utcnow(), up))
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    app = create_app(cfg.db_path, cfg.status_password, cfg.session_secret,
                     trigger=lambda: loop.call_soon_threadsafe(wake.set), warnings=warnings,
                     tz=cfg.timezone, max_jobs_per_day=cfg.max_jobs_per_day,
                     feedback_trigger=(lambda: loop.call_soon_threadsafe(queue.wake.set)) if queue is not None else None)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.status_bind, port=cfg.status_port, ssl_certfile=str(cfg.tls_dir / "cert.pem"),
        ssl_keyfile=str(cfg.tls_dir / "key.pem"), log_level="info"))
    loops = [poll_loop(poller, cfg.poll_interval_s, wake, conn), backup_loop(cfg, conn), server.serve(), capture.run()]
    if cfg.health_file is not None:
        loops.append(health_loop(conn, cfg))
    if queue is not None:
        loops.append(queue.run())
    if cfg.ma_layout_dir is not None:
        cover_ma = MaClient(cfg.ma_url, cfg.ma_token)  # own connection, as for capture
        musicbrainz = MusicBrainz(http, contact=cfg.contact_email)  # shared: it keeps all MusicBrainz requests to one per second
        # own connection with a long timeout: MA runs one metadata refresh per 30 s, so a refresh can wait minutes,
        # and it must not hold up the cover redraws on cover_ma
        artist_ma = MaClient(cfg.ma_url, cfg.ma_token, call_timeout_s=600)
        loops.append(played_artists_loop(PlayedArtists(conn, MaClient(cfg.ma_url, cfg.ma_token, call_timeout_s=600),
                                                       utcnow)))
        loops.append(artist_info_loop(ArtistInfoWorker(
            conn, artist_ma, musicbrainz, LastFm(http, cfg.lastfm_api_key), Wikipedia(http, cfg.contact_email), utcnow,
            lambda texts: write_artist_file(cfg.ma_layout_dir, texts))))
        loops.append(genre_loop(GenreWorker(
            conn, musicbrainz, utcnow,
            lambda: write_layout(conn, cfg.hub_sections, cfg.ma_layout_dir, cfg.cover_style, cfg.genre_hues,
                                 sound_path=cfg.sound_db_path, language=cfg.language),
            cover_ma.refresh_cover, cfg.cover_style, cfg.genre_hues,
            moods=lambda: playlist_moods(conn, cfg.sound_db_path),
            source_moods=lambda: source_moods(conn, cfg.sound_db_path))))
        if cfg.source_rows:
            loops.append(source_loop(SourceCollector(conn, MaClient(cfg.ma_url, cfg.ma_token), utcnow,
                                                     cfg.source_rows)))
    await asyncio.gather(*loops)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="bridge.main")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    load_dotenv(Path(".env"))
    asyncio.run(run(load_config(Path(args.config), os.environ)))


if __name__ == "__main__":
    main()
