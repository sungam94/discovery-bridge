#!/usr/bin/env python3
"""Example watchdog for the bridge's health.json, written for the Hermes agent's script cron. No LLM, no
network, no model call. Any scheduler that runs a script and forwards its output works the same way.

Run via: hermes cron create '*/15 * * * *' --script discovery_bridge.py --no-agent --deliver telegram:<chat>
Reads the health.json that the bridge writes every 5 min (config key health_file). Prints only on change (new
problem, recovery) and repeats an open problem once a day. Empty stdout = silent.

Environment: DISCOVERY_BRIDGE_HEALTH (path of health.json), DISCOVERY_BRIDGE_WATCH_STATE (where the watcher
keeps what it already reported), DISCOVERY_BRIDGE_LANG (en, the default, or de: the words around the problem
texts; the problem texts themselves come from health.json in the bridge's language).
"""
import datetime
import json
import os

HEALTH = os.environ.get("DISCOVERY_BRIDGE_HEALTH", "/opt/data/extern/discovery-bridge/health.json")
STATE = os.environ.get("DISCOVERY_BRIDGE_WATCH_STATE", "/opt/data/state/discovery-bridge-watch.json")
STALE_AFTER = datetime.timedelta(minutes=30)
REMIND_AFTER = datetime.timedelta(hours=24)
LANG = os.environ.get("DISCOVERY_BRIDGE_LANG", "en")
FRAMES = {
    "de": {"missing": "health.json fehlt oder ist unlesbar", "stale": "keine Statusmeldung seit {since}",
           "still": "weiterhin: ", "recovered": "wieder ok: "},
    "en": {"missing": "health.json is missing or unreadable", "stale": "no status update since {since}",
           "still": "still: ", "recovered": "ok again: "},
}


def _parse(ts):
    return datetime.datetime.fromisoformat(ts)


def check(health, now, state, language=None):
    """Returns (message, new_state). state maps problem key -> {"text", "since", "told"}."""
    words = FRAMES.get(language or LANG, FRAMES["en"])
    current = {}
    if health is None:
        current["bridge"] = words["missing"]
    else:
        written = _parse(health["written_at"])
        if now - written > STALE_AFTER:
            current["bridge"] = words["stale"].format(since=written.astimezone().strftime('%d.%m. %H:%M'))
        for p in health.get("problems", []):
            current[p["key"]] = p["text"]
    lines, new_state = [], {}
    for key, text in current.items():
        old = state.get(key)
        if old is None:
            lines.append(f"⚠ {text}")
            new_state[key] = {"text": text, "since": now.isoformat(), "told": now.isoformat()}
        elif now - _parse(old["told"]) >= REMIND_AFTER:
            lines.append(f"⚠ {words['still']}{text}")
            new_state[key] = {**old, "text": text, "told": now.isoformat()}
        else:
            new_state[key] = {**old, "text": text}
    for key, old in state.items():
        if key not in current:
            lines.append(f"✅ {words['recovered']}{old['text']}")
    if not lines:
        return "", new_state
    return "Discovery Bridge\n" + "\n".join(lines), new_state


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    message, state = check(_load(HEALTH), now, _load(STATE) or {})
    _save(STATE, state)
    if message:
        print(message)


if __name__ == "__main__":
    main()
