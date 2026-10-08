from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bridge.texts import check_language

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Genre labels and mood footer on the playlist covers (drawn by the MA plugin; sizes are pixels on a 1500 px
# cover). bands = genres shown, font / font_min = largest and smallest genre text size, darken = dimming of the
# cover collage (0..1), footer = height of the mood footer, grid = covers per side of the collage.
# The plugin (ma_provider/spotify_bridge) has the same defaults. Changing a value redraws all covers.
DEFAULT_COVER_STYLE = {"bands": 4, "font": 250, "font_min": 110, "darken": 0.62, "footer": 120, "footer2": 56,
                       "grid": 3}
# Sections of Spotify's "Made for you" hub whose playlists are mirrored too. The IDs are the same for every
# account and language. Other known sections: 0JQ5DACFo5h0jxzOyHOsI9 genre mixes, 0JQ5DACFo5h0jxzOyHOsIb decade
# mixes, 0JQ5DATaxswzruE2nWp3Lr niche mixes.
DEFAULT_HUB_SECTIONS = (
    "spotify:section:0JQ5DACFo5h0jxzOyHOsIe",  # Just for you (daylist, On Repeat, Repeat Rewind, ...)
    "spotify:section:0JQ5DACFo5h0jxzOyHOsIa",  # artist mixes
    "spotify:section:0JQ5DACFo5h0jxzOyHOsIc",  # mood mixes
    "spotify:section:0JQ5DACFo5h0jxzOyHOsIp",  # Blends
)
# Discover rows of other providers whose playlists get bridge covers too (a row counts when its name starts with
# one of the strings)
DEFAULT_SOURCE_ROWS = {"tidal": ("Custom mixes",), "soundcloud": ("Mixed for", "Made for you")}


@dataclass(frozen=True)
class Thresholds:
    """Spec §5.2 / §10: outcome thresholds."""
    completed: float = 0.9
    listened: float = 0.5
    listened_min_s: int = 30
    skipped_s: int = 30


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(text))
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"bad duration (must be > 0, e.g. 1h): {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


@dataclass(frozen=True)
class Config:
    ma_url: str
    ma_token: str
    sp_dc: str
    status_password: str
    session_secret: str
    data_dir: Path
    db_path: Path
    backup_dir: Path
    tls_dir: Path
    backup_copy_target: Path | None
    status_bind: str = "0.0.0.0"
    status_port: int = 8790
    poll_interval_s: int = 3600
    discover_weekly_id: str | None = None  # optional override; normally found in the user's hub
    release_radar_id: str | None = None
    publish_history: frozenset[str] = frozenset({"discover_weekly", "release_radar"})
    timezone: str = "UTC"
    request_delay_s: float = 0.5
    hub_sections: tuple[str, ...] = DEFAULT_HUB_SECTIONS
    ma_layout_dir: Path | None = None
    cover_style: dict = field(default_factory=lambda: dict(DEFAULT_COVER_STYLE))
    genre_hues: dict = field(default_factory=dict)  # genre -> hue (0-360), overrides the colour wheel
    lastfm_api_key: str | None = None  # artist bios for MA artist pages; without it only MusicBrainz/Wikipedia
    players_allowlist: frozenset[str] = frozenset()
    thresholds: Thresholds = Thresholds()
    health_file: Path | None = None
    max_jobs_per_day: int = 50
    job_attempts: int = 3
    max_job_age_s: int = 48 * 3600
    play_bucket_hours: int = 1          # a play is replayed in the same slot of the day (1 = the same hour)
    max_play_age_s: int = 14 * 86400    # until then it waits for its slot; the daily cap is spread over the day
    adapter_url: str = "http://127.0.0.1:8791"
    feedback_api_token: str | None = None
    source_rows: dict = field(default_factory=lambda: dict(DEFAULT_SOURCE_ROWS))
    language: str = "en"                # health.json problem texts and Discover row titles: en or de
    spotify_locale: str | None = None   # web player locale; default follows language (en-US, de-DE)
    status_hostname: str | None = None  # name in the status page certificate; default: the machine name
    contact_email: str | None = None    # added to the MusicBrainz and Wikipedia User-Agent when set

    @property
    def sound_db_path(self) -> Path:
        """Written by the sound-analysis container; the bridge only reads it."""
        return self.data_dir / "sound.sqlite"


def _source_rows(raw) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return dict(DEFAULT_SOURCE_ROWS)
    return {str(k): (v,) if isinstance(v, str) else tuple(str(x) for x in v or ()) for k, v in raw.items()}


def load_config(yaml_path: Path, env: Mapping[str, str]) -> Config:
    raw = yaml.safe_load(Path(yaml_path).read_text()) or {}
    data_dir = Path(raw.get("data_dir", "/data"))
    copy_target = raw.get("backup_copy_target")
    history = raw.get("publish_history", ["discover_weekly", "release_radar"])
    if isinstance(history, str):
        history = [history]
    allow = raw.get("players_allowlist") or []
    if isinstance(allow, str):
        allow = [allow]
    th = raw.get("thresholds") or {}
    return Config(
        ma_url=raw["ma_url"],
        ma_token=env["MA_TOKEN"],
        sp_dc=env["SPOTIFY_SP_DC"],
        status_password=env["STATUS_PASSWORD"],
        session_secret=env["SESSION_SECRET"],
        data_dir=data_dir,
        db_path=data_dir / "bridge.sqlite",
        backup_dir=data_dir / "backups",
        tls_dir=data_dir / "tls",
        backup_copy_target=Path(copy_target) if copy_target else None,
        status_bind=raw.get("status_bind", "0.0.0.0"),
        status_port=int(raw.get("status_port", 8790)),
        poll_interval_s=parse_duration(raw.get("poll_interval", "1h")),
        discover_weekly_id=raw.get("discover_weekly_id") or None,
        release_radar_id=raw.get("release_radar_id") or None,
        publish_history=frozenset(history),
        timezone=raw.get("timezone") or env.get("TZ") or "UTC",
        request_delay_s=float(raw.get("request_delay_s", 0.5)),
        hub_sections=tuple(raw.get("hub_sections", DEFAULT_HUB_SECTIONS)),
        ma_layout_dir=Path(raw["ma_layout_dir"]) if raw.get("ma_layout_dir") else None,
        cover_style={**DEFAULT_COVER_STYLE, **(raw.get("cover_style") or {})},
        genre_hues={str(k): float(v) for k, v in (raw.get("genre_hues") or {}).items()},
        players_allowlist=frozenset(str(p) for p in allow),
        thresholds=Thresholds(
            completed=float(th.get("completed", 0.9)), listened=float(th.get("listened", 0.5)),
            listened_min_s=int(th.get("listened_min_s", 30)), skipped_s=int(th.get("skipped_s", 30))),
        health_file=Path(raw["health_file"]) if raw.get("health_file") else None,
        max_jobs_per_day=int(raw.get("max_jobs_per_day", 50)),
        job_attempts=int(raw.get("job_attempts", 3)),
        max_job_age_s=parse_duration(raw.get("max_job_age", "48h")),
        play_bucket_hours=int(raw.get("play_bucket_hours", 1)),
        max_play_age_s=parse_duration(raw.get("max_play_age", "14d")),
        adapter_url=raw.get("adapter_url", "http://127.0.0.1:8791"),
        feedback_api_token=env.get("FEEDBACK_API_TOKEN") or None,
        lastfm_api_key=env.get("LASTFM_API_KEY") or None,
        source_rows=_source_rows(raw.get("source_rows")),
        language=check_language(str(raw.get("language", "en"))),
        spotify_locale=str(raw["spotify_locale"]) if raw.get("spotify_locale") else None,
        status_hostname=str(raw["status_hostname"]) if raw.get("status_hostname") else None,
        contact_email=str(raw["contact_email"]) if raw.get("contact_email") else None,
    )
