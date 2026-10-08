# Watchdog (optional)

The bridge runs unattended. When something breaks (the Spotify cookie expired, MA is unreachable, a backup
failed), you only notice if something tells you. That is what the optional `health.json` file and a watchdog
are for. Everything else works without them: the status page shows the same state when you look at it.

## 1. Turn on health.json

Run `scripts/setup.sh` and answer yes to the watchdog question, or set this in `config.yaml`:

```
health_file: /health/health.json
```

The bridge then writes `health.json` into the `HEALTH_DIR` folder from `.env` (default `./health`) every five
minutes. After changing `config.yaml`, run `docker compose up -d --force-recreate`.

```
{"version": 1, "written_at": "2026-10-08T09:30:00+00:00", "problems": [{"key": "spotify", "text": "..."}]}
```

- `written_at` is in UTC. A file older than about 30 minutes means the bridge is not running.
- Each problem has a `key` that is the same in every language and a `text` in the configured `language`:

| Key | Meaning |
|---|---|
| `spotify` | the Spotify cookie expired or Spotify refuses the requests |
| `poll` | the playlist check failed several times in a row, or has not succeeded for too long |
| `playlist:<slot>` | one playlist could not be read or published |
| `capture` | the bridge lost its connection to MA's events, so plays are not recorded |
| `backup` | the daily backup failed or is too old |
| `adapter`, `jobs`, `queue` | the feedback adapter is down, or its jobs fail or pile up |
| `genre_wheel` | many cover genres have no fixed colour yet; the genre colour wheel should be recomputed |
| `sound` | the sound analysis wrote nothing for 6 hours although tracks are waiting |
| `disk` | the data folder is running out of space |

No secrets go into the file.

## 2. Pick a watchdog

Anything that can read a file on a schedule and send you a message works. `ops/hermes/discovery_bridge.py` is a
small script that does the comparing for you: it prints a line only when a problem appears, repeats an open
problem once a day, and prints "ok again" when it clears. When all is well it prints nothing. It needs only
Python 3, no packages, no network.

It reads three environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DISCOVERY_BRIDGE_HEALTH` | `/opt/data/extern/discovery-bridge/health.json` | where `health.json` is |
| `DISCOVERY_BRIDGE_WATCH_STATE` | `/opt/data/state/discovery-bridge-watch.json` | where the script remembers what it already reported |
| `DISCOVERY_BRIDGE_LANG` | `en` | `en` or `de` for the words around the problem texts |

The defaults fit Hermes (below); for anything else set the first two.

### Option A: Hermes

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is a self-hosted assistant with a scheduler that
can deliver messages to Telegram and other chat apps. Its script jobs can run without the language model
(`--no-agent`), so watching the bridge costs no model calls. Use it if you already run Hermes; it is not worth
installing just for this.

1. Point `HEALTH_DIR` in the bridge's `.env` at a folder inside Hermes' data folder, so Hermes sees the file
   at `/opt/data/extern/discovery-bridge/`. Hermes' data folder is usually `~/.hermes`, mounted at `/opt/data`
   in its container; write it out in full, because Compose does not expand `~`:
   ```
   HEALTH_DIR=/path/to/.hermes/extern/discovery-bridge
   ```
   Then `docker compose up -d --force-recreate`.
2. Copy the script into Hermes' scripts folder:
   ```
   cp ops/hermes/discovery_bridge.py ~/.hermes/scripts/
   ```
3. Create the job, every 15 minutes, delivered to your Telegram chat:
   ```
   hermes cron create '*/15 * * * *' --script discovery_bridge.py --no-agent --deliver telegram:<chat>
   ```
4. Test it: stop the bridge (`docker compose stop discovery-bridge`). Within about 45 minutes Hermes sends
   "no status update since …". Start it again and you get "ok again".

When you update the bridge, copy the script again if `ops/hermes/discovery_bridge.py` changed.

### Option B: cron and mail

On a server whose cron can send mail, cron mails any output of a job, and the script prints only on changes:

```
MAILTO=you@example.org
*/15 * * * * DISCOVERY_BRIDGE_HEALTH=/path/to/discovery-bridge/health/health.json DISCOVERY_BRIDGE_WATCH_STATE=/path/to/discovery-bridge/health/watch-state.json python3 /path/to/discovery-bridge/ops/hermes/discovery_bridge.py
```

### Option C: cron and a push notification

Without mail, send the output to a push service such as [ntfy](https://ntfy.sh) (pick a topic name nobody can
guess, or run your own ntfy server):

```
*/15 * * * * out=$(DISCOVERY_BRIDGE_HEALTH=/path/to/discovery-bridge/health/health.json DISCOVERY_BRIDGE_WATCH_STATE=/path/to/discovery-bridge/health/watch-state.json python3 /path/to/discovery-bridge/ops/hermes/discovery_bridge.py) && [ -n "$out" ] && curl -s -d "$out" https://ntfy.sh/<your-topic> >/dev/null
```

### Option D: your own monitoring

Home Assistant, Uptime Kuma, Node-RED or a script of your own can read `health.json` directly: alert when
`problems` is not empty, or when `written_at` is older than 30 minutes.

## Turning it off

Answer no in `scripts/setup.sh`, or comment out `health_file` in `config.yaml`, then
`docker compose up -d --force-recreate`, and remove the watchdog job. If you leave a watchdog running without
the file, it reports "health.json is missing".
