"""bridge.cli publish-once: ingests and publishes one fixed slot with the same settings the service uses."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bridge.cli as cli
from bridge.config import load_config
from bridge.ma.client import MaClient
from tests.ma.fake_server import FakeMa
from tests.publish.test_publisher import add_snapshot

LATE = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)  # 01:30 on the 28th in Berlin
ENV = {"MA_TOKEN": "t", "SPOTIFY_SP_DC": "c", "STATUS_PASSWORD": "p", "SESSION_SECRET": "s"}


class FakeIngestor:
    """Stands in for Spotify and the resolver: one fully matched snapshot, fetched at the patched clock."""

    def __init__(self, conn, spotify, resolver, now, delay):
        self.conn, self.now = conn, now

    async def ingest_slot(self, slot, spotify_id):
        sid = add_snapshot(self.conn, slot, [("a", "matched", "tidal--test0001://track/1")], set_hash="S1")
        self.conn.execute("UPDATE playlist_snapshot SET fetched_at = ? WHERE id = ?", (self.now().isoformat(), sid))
        return "snapshot"


@pytest.fixture
def fake_ma(monkeypatch):
    holder = {}

    def make_client(url, token):
        return MaClient(holder["fake"].url, "good")

    monkeypatch.setattr(cli, "MaClient", make_client)
    monkeypatch.setattr(cli, "make_session", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "Ingestor", FakeIngestor)
    monkeypatch.setattr(cli, "utcnow", lambda: LATE)
    return holder


def config(tmp_path: Path, extra: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    y = tmp_path / "config.yaml"
    y.write_text(f"ma_url: ws://unused/ws\ndata_dir: {tmp_path / 'data'}\n"
                 "discover_weekly_id: 37i9dQZEVXcFakeDw00001\n" + extra)
    return load_config(y, ENV)


async def run(holder, cfg):
    async with FakeMa() as fake:
        holder["fake"] = fake
        await cli.publish_once(cfg, "discover_weekly")
        return sorted(fake.names.values())


async def test_publish_once_creates_history_with_the_configured_settings(tmp_path, fake_ma):
    names = await run(fake_ma, config(tmp_path, "timezone: Europe/Berlin\n"))
    assert "Spotify · Discover Weekly" in names
    assert any(n.startswith("Spotify · DW ") for n in names)


async def test_publish_once_dates_history_in_the_configured_timezone(tmp_path, fake_ma):
    berlin = await run(fake_ma, config(tmp_path / "b", "timezone: Europe/Berlin\n"))
    utc = await run(fake_ma, config(tmp_path / "u", "timezone: UTC\n"))
    assert "Spotify · DW 2026-09-28" in berlin
    assert "Spotify · DW 2026-09-27" in utc


class FakeSpotify:
    def __init__(self, source, http):
        pass

    async def made_for_you(self):
        return {"spotify:section:x": [("37i9dQZEVXcFakeDw00009", "Discover Weekly")]}


async def test_publish_once_takes_the_id_from_the_hub_without_an_override(tmp_path, fake_ma, monkeypatch):
    seen = []

    class RecordingIngestor(FakeIngestor):
        async def ingest_slot(self, slot, spotify_id):
            seen.append((slot, spotify_id))
            return await super().ingest_slot(slot, spotify_id)

    monkeypatch.setattr(cli, "SpotifyClient", FakeSpotify)
    monkeypatch.setattr(cli, "Ingestor", RecordingIngestor)
    tmp_path.mkdir(parents=True, exist_ok=True)
    y = tmp_path / "config.yaml"
    y.write_text(f"ma_url: ws://unused/ws\ndata_dir: {tmp_path / 'data'}\n")
    await run(fake_ma, load_config(y, ENV))
    assert seen == [("discover_weekly", "37i9dQZEVXcFakeDw00009")]
