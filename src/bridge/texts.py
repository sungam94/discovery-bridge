"""User-facing texts in the configured language (config key `language`): the problem texts in health.json and
the Discover row titles in layout.json. Problem keys and row ids are the same in every language; only the
texts change. Every language has exactly the same keys (tests/test_texts.py checks this)."""
from __future__ import annotations

LANGUAGES = ("en", "de")

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # health.json problems
        "spotify_auth_expired": "Spotify login expired: renew the sp_dc cookie",
        "spotify_error": "Spotify: hub not reachable",
        "poll_failures": "Poll failed {failures} times: {error}",
        "poll_overdue": "No successful poll since {since}",
        "capture": "MA event connection down since {since}",
        "backup_error": "Backup failed: {error}",
        "backup_age": "Last backup {at}",
        "adapter": "Spotify adapter {state} since {since}: {detail}",
        "jobs": "{failed} feedback jobs failed in 24 h",
        "queue": "Feedback job running since {since}",
        "genre_wheel": "Recompute the genre colour wheel: {percent} % of the cover genres have no fixed place "
                       "({names})",
        "sound": "Sound analysis stalled since {since} ({waiting} waiting)",
        "disk": "Low disk space: {free_gb:.1f} GB free",
        # layout.json Discover row titles, by row id
        "for_you": "Spotify · Made for you",
        "daily_mixes": "Spotify · Daily Mixes",
        "section_0JQ5DACFo5h0jxzOyHOsIa": "Spotify · Artist mixes",
        "section_0JQ5DACFo5h0jxzOyHOsIc": "Spotify · Mood mixes",
        "section_0JQ5DACFo5h0jxzOyHOsIp": "Spotify · Blends",
        "section_0JQ5DACFo5h0jxzOyHOsI9": "Spotify · Genre mixes",
        "section_0JQ5DACFo5h0jxzOyHOsIb": "Spotify · Decade mixes",
        "section_0JQ5DATaxswzruE2nWp3Lr": "Spotify · Niche mixes",
        "section_other": "Spotify · Mixes",
    },
    "de": {
        "spotify_auth_expired": "Spotify-Anmeldung abgelaufen: sp_dc-Cookie erneuern",
        "spotify_error": "Spotify: Hub nicht abrufbar",
        "poll_failures": "Abruf {failures}x fehlgeschlagen: {error}",
        "poll_overdue": "kein erfolgreicher Abruf seit {since}",
        "capture": "MA-Ereignisverbindung getrennt seit {since}",
        "backup_error": "Backup fehlgeschlagen: {error}",
        "backup_age": "letztes Backup {at}",
        "adapter": "Spotify-Adapter {state} seit {since}: {detail}",
        "jobs": "{failed} Feedback-Jobs in 24 h fehlgeschlagen",
        "queue": "Feedback-Job läuft seit {since}",
        "genre_wheel": "Genre-Farbkreis neu berechnen: {percent} % der Cover-Genres ohne festen Platz ({names})",
        "sound": "Klanganalyse steht seit {since} ({waiting} Titel offen)",
        "disk": "wenig Speicher: {free_gb:.1f} GB frei",
        "for_you": "Spotify · Für dich",
        "daily_mixes": "Spotify · Daily Mixes",
        "section_0JQ5DACFo5h0jxzOyHOsIa": "Spotify · Künstler-Mixe",
        "section_0JQ5DACFo5h0jxzOyHOsIc": "Spotify · Stimmungs-Mixe",
        "section_0JQ5DACFo5h0jxzOyHOsIp": "Spotify · Blends",
        "section_0JQ5DACFo5h0jxzOyHOsI9": "Spotify · Genre-Mixe",
        "section_0JQ5DACFo5h0jxzOyHOsIb": "Spotify · Jahrzehnte-Mixe",
        "section_0JQ5DATaxswzruE2nWp3Lr": "Spotify · Nischen-Mixe",
        "section_other": "Spotify · Mixe",
    },
}


def check_language(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}, not {language!r}")
    return language


def has_text(language: str, key: str) -> bool:
    return key in TEXTS[check_language(language)]


def text(language: str, key: str, **values) -> str:
    return TEXTS[check_language(language)][key].format(**values)
