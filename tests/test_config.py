from pathlib import Path

import pytest

from bridge.config import Thresholds, load_config, parse_duration

ENV = {"MA_TOKEN": "t", "SPOTIFY_SP_DC": "c", "STATUS_PASSWORD": "p", "SESSION_SECRET": "s"}


def test_parse_duration():
    assert parse_duration("45s") == 45
    assert parse_duration("30m") == 1800
    assert parse_duration("1h") == 3600
    assert parse_duration("7d") == 604800


@pytest.mark.parametrize("bad", ["soon", "0s", "0h", ""])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_load_config(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text(
        "ma_url: ws://127.0.0.1:8095/ws\n"
        "data_dir: /data\n"
        "status_bind: 192.0.2.10\n"
        "poll_interval: 1h\n"
        "backup_copy_target: /backup-copy\n"
    )
    cfg = load_config(y, ENV)
    assert cfg.ma_url == "ws://127.0.0.1:8095/ws"
    assert cfg.db_path == Path("/data/bridge.sqlite")
    assert cfg.backup_dir == Path("/data/backups")
    assert cfg.tls_dir == Path("/data/tls")
    assert cfg.backup_copy_target == Path("/backup-copy")
    assert cfg.poll_interval_s == 3600
    assert cfg.discover_weekly_id is None and cfg.release_radar_id is None
    assert cfg.publish_history == frozenset({"discover_weekly", "release_radar"})
    assert cfg.ma_token == "t" and cfg.sp_dc == "c"
    assert cfg.hub_sections == ("spotify:section:0JQ5DACFo5h0jxzOyHOsIe", "spotify:section:0JQ5DACFo5h0jxzOyHOsIa",
                                "spotify:section:0JQ5DACFo5h0jxzOyHOsIc", "spotify:section:0JQ5DACFo5h0jxzOyHOsIp")
    assert cfg.ma_layout_dir is None


def test_hub_sections_and_layout_dir(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\nhub_sections: [spotify:section:A]\nma_layout_dir: /ma_layout\n")
    cfg = load_config(y, ENV)
    assert cfg.hub_sections == ("spotify:section:A",) and cfg.ma_layout_dir == Path("/ma_layout")


def test_publish_history_scalar_is_one_item(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x\npublish_history: discover_weekly\n")
    assert load_config(y, ENV).publish_history == frozenset({"discover_weekly"})


def test_missing_secret_fails(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x\ndata_dir: /data\n")
    with pytest.raises(KeyError, match="SPOTIFY_SP_DC"):
        load_config(y, {k: v for k, v in ENV.items() if k != "SPOTIFY_SP_DC"})


def test_capture_defaults(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    cfg = load_config(y, ENV)
    assert cfg.players_allowlist == frozenset()
    assert cfg.thresholds == Thresholds(completed=0.9, listened=0.5, listened_min_s=30, skipped_s=30)


def test_capture_keys(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n"
                 "players_allowlist: [00:00:5e:00:53:01, up00000001]\n"
                 "thresholds: {completed: 0.85, skipped_s: 20}\n")
    cfg = load_config(y, ENV)
    assert cfg.players_allowlist == frozenset({"00:00:5e:00:53:01", "up00000001"})
    assert cfg.thresholds == Thresholds(completed=0.85, listened=0.5, listened_min_s=30, skipped_s=20)


def test_health_file(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\nhealth_file: /health/health.json\n")
    assert load_config(y, ENV).health_file == Path("/health/health.json")
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).health_file is None


def test_feedback_defaults(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    cfg = load_config(y, ENV)
    assert (cfg.max_jobs_per_day, cfg.job_attempts, cfg.max_job_age_s) == (50, 3, 48 * 3600)
    assert cfg.adapter_url == "http://127.0.0.1:8791" and cfg.feedback_api_token is None


def test_feedback_keys(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\nmax_jobs_per_day: 20\njob_attempts: 2\nmax_job_age: 24h\n"
                 "adapter_url: http://127.0.0.1:9000\n")
    cfg = load_config(y, {**ENV, "FEEDBACK_API_TOKEN": "f"})
    assert (cfg.max_jobs_per_day, cfg.job_attempts, cfg.max_job_age_s) == (20, 2, 24 * 3600)
    assert cfg.adapter_url == "http://127.0.0.1:9000" and cfg.feedback_api_token == "f"


def test_cover_style_defaults_and_overrides(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\ncover_style:\n  grid: 4\n")
    cfg = load_config(y, ENV)
    assert cfg.cover_style == {"bands": 4, "font": 250, "font_min": 110, "darken": 0.62, "footer": 120, "footer2": 56,
                               "grid": 4}


def test_sound_database_lives_in_the_data_dir(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\ndata_dir: /opt/d\n")
    assert load_config(y, ENV).sound_db_path == Path("/opt/d/sound.sqlite")


def test_genre_hue_overrides(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\ngenre_hues:\n  jazz: 99\n")
    assert load_config(y, ENV).genre_hues == {"jazz": 99.0}
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).genre_hues == {}


def test_lastfm_key_comes_from_the_environment(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).lastfm_api_key is None
    assert load_config(y, {**ENV, "LASTFM_API_KEY": "abc"}).lastfm_api_key == "abc"


def test_replay_slot_and_play_age(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    cfg = load_config(y, ENV)
    assert (cfg.play_bucket_hours, cfg.max_play_age_s) == (1, 14 * 86400)
    y.write_text("ma_url: ws://x/ws\nplay_bucket_hours: 2\nmax_play_age: 7d\n")
    cfg = load_config(y, ENV)
    assert (cfg.play_bucket_hours, cfg.max_play_age_s) == (2, 7 * 86400)


def test_source_rows_default_and_override(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).source_rows == {"tidal": ("Custom mixes",), "soundcloud": ("Mixed for", "Made for you")}
    y.write_text("ma_url: ws://x/ws\nsource_rows:\n  tidal: [Custom mixes, Daily]\n  soundcloud: Mixed for\n")
    assert load_config(y, ENV).source_rows == {"tidal": ("Custom mixes", "Daily"), "soundcloud": ("Mixed for",)}
    y.write_text("ma_url: ws://x/ws\nsource_rows: {}\n")
    assert load_config(y, ENV).source_rows == {}


def test_explicit_keys_are_honoured(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n"
                 "timezone: America/New_York\n"
                 "discover_weekly_id: 37i9dQZEVXcFakeDw00001\n"
                 "release_radar_id: 37i9dQZEVXbFakeRr00001\n"
                 "status_bind: 192.0.2.10\n"
                 "hub_sections: [spotify:section:A, spotify:section:B]\n"
                 "source_rows:\n  tidal: [Mixes]\n")
    cfg = load_config(y, ENV)
    assert cfg.timezone == "America/New_York"
    assert (cfg.discover_weekly_id, cfg.release_radar_id) == ("37i9dQZEVXcFakeDw00001", "37i9dQZEVXbFakeRr00001")
    assert cfg.status_bind == "192.0.2.10"
    assert cfg.hub_sections == ("spotify:section:A", "spotify:section:B")
    assert cfg.source_rows == {"tidal": ("Mixes",)}


def test_timezone_falls_back_to_tz_then_utc(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).timezone == "UTC"
    assert load_config(y, {**ENV, "TZ": "Europe/Berlin"}).timezone == "Europe/Berlin"
    y.write_text("ma_url: ws://x/ws\ntimezone: America/New_York\n")
    assert load_config(y, {**ENV, "TZ": "Europe/Berlin"}).timezone == "America/New_York"


def test_language_defaults_to_english_and_accepts_german(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).language == "en"
    y.write_text("ma_url: ws://x/ws\nlanguage: de\n")
    assert load_config(y, ENV).language == "de"


def test_unknown_language_is_rejected(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\nlanguage: fr\n")
    with pytest.raises(ValueError, match="language"):
        load_config(y, ENV)


def test_spotify_locale_is_an_optional_override(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    assert load_config(y, ENV).spotify_locale is None
    y.write_text("ma_url: ws://x/ws\nspotify_locale: fr-FR\n")
    assert load_config(y, ENV).spotify_locale == "fr-FR"


def test_status_hostname_and_contact_email_are_optional(tmp_path: Path):
    y = tmp_path / "config.yaml"
    y.write_text("ma_url: ws://x/ws\n")
    cfg = load_config(y, ENV)
    assert cfg.status_hostname is None and cfg.contact_email is None
    y.write_text("ma_url: ws://x/ws\nstatus_hostname: homeserver\ncontact_email: someone@example.org\n")
    cfg = load_config(y, ENV)
    assert cfg.status_hostname == "homeserver" and cfg.contact_email == "someone@example.org"
