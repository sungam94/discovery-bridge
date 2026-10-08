"""config.example.yaml and .env.example are what a new installation starts from: they must load, list every
setting, and carry no address of one particular network."""
import re
from pathlib import Path

from bridge.config import load_config

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "config.example.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCUMENTED = [  # every config.yaml key; optional ones may be commented out ("# key:")
    "ma_url", "data_dir", "language", "spotify_locale", "timezone", "status_bind", "status_port", "status_hostname",
    "poll_interval", "publish_history", "backup_copy_target", "request_delay_s", "health_file", "ma_layout_dir",
    "players_allowlist", "hub_sections", "source_rows", "discover_weekly_id", "release_radar_id", "adapter_url",
    "max_jobs_per_day", "job_attempts", "max_job_age", "play_bucket_hours", "max_play_age", "thresholds",
    "cover_style", "genre_hues", "contact_email",
]
ENV_VARS = ["MA_TOKEN", "SPOTIFY_SP_DC", "STATUS_PASSWORD", "SESSION_SECRET", "FEEDBACK_API_TOKEN",
            "LASTFM_API_KEY", "TZ", "BACKUP_COPY_DIR", "HEALTH_DIR", "COMPOSE_PROFILES"]


def env_example() -> dict[str, str]:
    out = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_config_example_loads_with_the_friend_defaults():
    env = {k: v or "x" for k, v in env_example().items()}
    cfg = load_config(EXAMPLE, env)
    assert cfg.language == "en" and cfg.status_bind == "0.0.0.0"
    assert cfg.ma_layout_dir == Path("/ma_layout") and cfg.backup_copy_target == Path("/backup-copy")
    assert cfg.discover_weekly_id is None and cfg.release_radar_id is None
    assert cfg.players_allowlist == frozenset()
    assert cfg.health_file is None  # the watchdog file is opt-in (scripts/setup.sh asks)


def test_config_example_documents_every_key():
    text = EXAMPLE.read_text()
    missing = [k for k in DOCUMENTED if not re.search(rf"^#? ?{k}:", text, re.M)]
    assert missing == []


def test_env_example_lists_every_variable_and_no_value():
    env = env_example()
    assert set(ENV_VARS) <= set(env)
    secrets = ["MA_TOKEN", "SPOTIFY_SP_DC", "STATUS_PASSWORD", "SESSION_SECRET", "FEEDBACK_API_TOKEN", "LASTFM_API_KEY"]
    assert all(env[k] == "" for k in secrets)


def test_every_compose_variable_is_in_the_env_example():
    compose = (ROOT / "docker-compose.yml").read_text()
    used = set(re.findall(r"\$\{([A-Z_]+)", compose))
    assert used <= set(env_example())


def test_examples_have_no_private_network_address():
    for path in (EXAMPLE, ENV_EXAMPLE):
        assert not re.search(r"\b(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+", path.read_text()), path
