"""docker-compose.yml: the services, their network mode and the container side of every mount. Only mount
targets are checked; the host side is each installation's own choice."""
from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())


def split_volume(volume: str) -> tuple[str, str]:
    """(host source, container target) of a short volume entry; the source may be a ${VAR:-default} expression,
    which itself contains a colon."""
    if volume.startswith("${"):
        end = volume.index("}") + 1
        source, rest = volume[:end], volume[end + 1:]
    else:
        source, _, rest = volume.partition(":")
    return source, rest.split(":")[0]


def targets(service: str) -> list[str]:
    return [split_volume(v)[1] for v in COMPOSE["services"][service].get("volumes", [])]


def test_compose_services_and_mount_targets():
    services = COMPOSE["services"]
    assert set(services) == {"discovery-bridge", "spotify-feedback", "sound-analysis"}
    assert services["discovery-bridge"]["network_mode"] == "host"
    assert services["spotify-feedback"]["network_mode"] == "host"
    assert targets("discovery-bridge") == ["/data", "/app/config.yaml", "/ma_layout", "/backup-copy", "/health"]
    assert {"SPOTIFY_SP_DC", "FEEDBACK_API_TOKEN", "TZ"} <= set(services["spotify-feedback"]["environment"])
    assert targets("spotify-feedback") == ["/profile"]
    assert targets("sound-analysis") == ["/data"]


ROOT = Path(__file__).parents[2]


def test_compose_has_no_absolute_host_paths():
    for name, service in COMPOSE["services"].items():
        for volume in service.get("volumes", []):
            source, _ = split_volume(volume)
            if source.startswith("${"):
                assert ":-./" in source, (name, volume)  # a variable with a relative default
            else:
                assert source.startswith("./"), (name, volume)


def test_optional_services_are_behind_profiles():
    services = COMPOSE["services"]
    assert "profiles" not in services["discovery-bridge"]
    assert services["spotify-feedback"]["profiles"] == ["feedback"]
    assert services["sound-analysis"]["profiles"] == ["analysis"]


def test_every_service_takes_its_timezone_from_tz():
    for service in COMPOSE["services"].values():
        assert service["environment"]["TZ"] == "${TZ:-UTC}"
    for dockerfile in ("Dockerfile", "adapter/Dockerfile", "analysis/Dockerfile"):
        assert "TZ=" not in (ROOT / dockerfile).read_text()
