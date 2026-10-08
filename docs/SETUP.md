# Setting up Discovery Bridge

This guide installs your own copy on your own home server, with your own Music Assistant, Spotify and TIDAL
accounts. Nothing in it connects to anyone else's server.

## Spotify terms: read first

- Spotify's terms forbid automated access to the service and anything that artificially inflates play counts.
- **The bridge** reads the web player's private API with your login cookie: personal, read-only, about one read
  an hour with a pause between requests. It is still against the terms.
- **The feedback adapter** goes further: a real Chrome browser plays tracks and saves likes on your account to
  reproduce what you heard in MA. That is automated playback, the kind of activity Spotify looks for. The risk
  is a suspended or closed account.
- Use an account you can afford to lose, keep the default limit (`max_jobs_per_day: 50`), and never point the
  bridge at someone else's account.
- **Running without the adapter** is the default: `feedback` is not in `COMPOSE_PROFILES` and
  `FEEDBACK_API_TOKEN` is empty. The bridge still mirrors the playlists, draws the covers and records your plays
  locally; nothing is played on Spotify. There is no mode without the cookie, because the personal playlists are
  only reachable when logged in.

## 1. Prerequisites

- A **Linux** server with Docker and Docker Compose v2 (`docker compose`, not `docker-compose`). The services
  use `network_mode: host`, which does not work in Docker Desktop on macOS or Windows.
- **x86_64** for the feedback adapter (Google Chrome with Widevine) and the sound analysis (the
  `essentia-tensorflow` wheel). The bridge alone also runs on ARM, such as a Raspberry Pi.
- **Music Assistant 2.10.x running in Docker on the same server.** The bridge and the MA plugin share a folder
  through bind mounts, and the bridge connects to MA's API on port 8095.
- **TIDAL set up in MA** and logged in: the mirrored playlists are made of TIDAL tracks.
- Optional: SoundCloud in MA, if you want its mixes to get the bridge's covers too.
- `git`, `bash` and `curl` on the server; `openssl` is used when present.

## 2. Accounts and keys

- A **Spotify Free** account: the one whose taste is used. Its personal playlists are what gets mirrored.
- **TIDAL** in Music Assistant (see above).
- A **Music Assistant long-lived token**: in MA, Settings > Profile > Long-lived tokens.
- Optional: a **Last.fm API key** (https://www.last.fm/api/account/create) for longer artist biographies on MA
  artist pages. Without it the bridge uses MusicBrainz and Wikipedia only.

## 3. Get the Spotify cookie

The bridge and the adapter log in to Spotify by using your browser's login cookie, `sp_dc`. There is no login
screen to fill in on the server.

1. On a desktop computer, open https://open.spotify.com and log in with the Spotify account from step 2.
2. Open the browser's developer tools: Chrome or Edge: Application > Cookies; Firefox: Storage > Cookies.
3. Select `https://open.spotify.com` and copy the **value** of the cookie named `sp_dc`.
4. Treat it like a password. Anyone who has it is logged in to your account.

The cookie lasts about a year. When it expires, `health.json` reports the problem key `spotify` ("Spotify login
expired: renew the sp_dc cookie"), and you repeat these steps (see Troubleshooting).

## 4. Get the code and run the setup script

Put the repository on the server, for example next to your Music Assistant folder, and run the setup script in
it:

```
cd discovery-bridge
scripts/setup.sh
```

The script:

- creates `.env` from `.env.example` and `config.yaml` from `config.example.yaml` if they are missing (both are
  ignored by git), and keeps `.env` at mode 600;
- asks for the MA token, the `sp_dc` cookie, a status page password and an optional Last.fm key, without showing
  what you type; it generates `SESSION_SECRET` itself;
- asks for the server's timezone (`TZ`), whether to run the feedback adapter (after showing the terms note; the
  default is no) and the sound analysis, and sets `COMPOSE_PROFILES` and `FEEDBACK_API_TOKEN` to match;
- asks for MA's address (`ma_url`; `127.0.0.1` when MA runs on this server), the language (`en` or `de`) and,
  optionally, the MA players whose plays count as your taste (`players_allowlist`, see step 7);
- creates `data/`, `chrome-profile/`, `backup-copy/`, `health/` and `ma_provider/spotify_bridge/state/` as your
  user, so Docker does not create them owned by root.

Every question shows the current value as its default, so you can run the script again at any time to change
one answer; pressing Enter keeps the rest. Secrets are only shown as "set".

**By hand instead:** `cp .env.example .env`, `chmod 600 .env`, `cp config.example.yaml config.yaml`, and fill
in the values. Both example files explain every setting.

The settings you are most likely to look at later:

| File | Setting | What it does |
|---|---|---|
| `.env` | `MA_TOKEN`, `SPOTIFY_SP_DC`, `STATUS_PASSWORD`, `SESSION_SECRET` | required secrets |
| `.env` | `FEEDBACK_API_TOKEN` | shared by the bridge and the adapter; empty turns feedback off |
| `.env` | `LASTFM_API_KEY` | optional, artist biographies |
| `.env` | `TZ` | timezone of all containers and of the bridge's schedules (default `UTC`) |
| `.env` | `COMPOSE_PROFILES` | `feedback`, `analysis`, both (`feedback,analysis`) or empty |
| `.env` | `BACKUP_COPY_DIR`, `HEALTH_DIR` | host folders for the second backup copy and for `health.json` |
| `config.yaml` | `ma_url` | MA's WebSocket address, `ws://127.0.0.1:8095/ws` on the same server |
| `config.yaml` | `language` | `en` or `de`: Discover row titles, health texts and the Spotify locale |
| `config.yaml` | `players_allowlist` | MA players whose plays count as taste |
| `config.yaml` | `source_rows` | TIDAL and SoundCloud Discover rows that get the bridge's covers; `{}` turns it off |
| `config.yaml` | `max_jobs_per_day` | daily limit of plays and likes the adapter does on Spotify |

## 5. Install the Music Assistant plugin

The plugin (`ma_provider/spotify_bridge`) runs inside your MA container. Add a read-only volume to your MA
service that mounts this folder into MA's providers directory, next to MA's built-in providers.

First find that directory. This command asks MA's own Python where its providers live (replace
`music-assistant` with the name of your MA container; this step was written from MA's package layout and not
run against a live MA):

```
docker exec music-assistant python3 -c "import os, music_assistant.providers as p; print(os.path.dirname(p.__file__))"
```

Then add the volume to your MA service in its compose file (the left side is where this repository is on the
server, the right side is the printed directory plus `/spotify_bridge`):

```
    volumes:
      - /path/to/discovery-bridge/ma_provider/spotify_bridge:<printed directory>/spotify_bridge:ro
```

The bridge writes its files into the `state/` subfolder of that same folder (inside the bridge container it is
`/ma_layout`, set by `ma_layout_dir` in `config.yaml`), and MA reads them through this mount. Recreate the MA
container (`docker compose up -d` in MA's folder), then add or enable **Spotify Bridge** under Settings >
Providers in MA.

## 6. Start

In the repository folder:

```
docker compose up -d --build
```

This starts the bridge, plus the adapter and the sound analysis if `COMPOSE_PROFILES` in `.env` names them. The
first build of the adapter and the analysis images takes a while; the analysis image downloads the Essentia
models. What the sound analysis needs, how long its first run takes and whether to turn it on is in
[SOUND.md](SOUND.md).

## 7. Choose the players that count

Until `players_allowlist` in `config.yaml` lists at least one MA player, no plays count as taste and nothing is
fed back to Spotify. To find a player's id, open MA > Settings > Players and click the player: the id is the last
part of the page address. Add the ids, for example:

```
players_allowlist: ["first-player-id", "second-player-id"]
```

Keep the quotes: ids that look like MAC addresses would otherwise not be read as text. Running
`scripts/setup.sh` again and entering the ids does the same. Then apply the change with
`docker compose up -d --force-recreate`.

## 8. Check that it works

- `scripts/setup.sh --check` lists which settings are filled in (never their values), whether MA answers at
  `ma_url`, and whether the plugin's state folder exists.
- `docker compose ps` lists `discovery-bridge` (and `spotify-feedback` and `sound-analysis` if you turned them
  on) as running.
- `docker compose logs -f discovery-bridge` shows `migrations applied` at the start, then a line starting with
  `poll:` after the first read of Spotify. Startup warnings name settings that are still missing, such as an
  empty `players_allowlist`.
- The status page at `https://<server-ip>:8790` asks for the status page password. The certificate is
  self-signed, so the browser warns once.
- Within about an hour, `Spotify · Discover Weekly`, `Spotify · Release Radar` and the Daily Mixes appear in
  MA's library under Playlists. The Discover rows (`Spotify · Made for you` and the others) follow after the
  first poll, once the plugin has read the bridge's layout file.
- If you use `health.json` (step 9), its `problems` list is empty.

## 9. Monitoring with health.json

With `health_file: /health/health.json` in `config.yaml` (the default in the example), the bridge writes
`health.json` into the `HEALTH_DIR` folder every five minutes:

```
{"version": 1, "written_at": "2026-10-08T09:30:00+00:00", "problems": [{"key": "spotify", "text": "..."}]}
```

- `written_at` is the time of writing in UTC. A file older than about 30 minutes means the bridge is not
  running.
- Each problem has a `key` that is the same in every language (`spotify`, `poll`, `capture`, `backup`,
  `adapter`, `jobs`, `queue`, `genre_wheel`, `sound`, `disk`) and a `text` in the configured `language`.
- No secrets go into the file.

Any scheduler can watch it. `ops/hermes/discovery_bridge.py` is an example watchdog that prints only when a
problem appears or clears, so a plain cron job that mails its output is enough:

```
*/15 * * * * DISCOVERY_BRIDGE_HEALTH=/path/to/discovery-bridge/health/health.json DISCOVERY_BRIDGE_WATCH_STATE=/path/to/watch-state.json DISCOVERY_BRIDGE_LANG=en python3 /path/to/discovery-bridge/ops/hermes/discovery_bridge.py
```

Set `DISCOVERY_BRIDGE_LANG=en` for English wording around the problem texts; the script's default is German.

## 10. Updating, backups and moving

**Updating:** get the new version of your copy (for example `git pull`), then run `docker compose up -d --build`.
Database changes are applied automatically at start (`migrations applied` in the log). If the plugin changed,
restart MA as well, because MA loads providers only at start.

**After editing `.env` or `config.yaml`:** run `docker compose up -d --force-recreate`. A plain restart keeps
the old environment, and editors often replace `config.yaml` with a new file that a running container does not
see.

**What lives where:**

- `data/bridge.sqlite`: the bridge's database (playlists, track matches, taste events, feedback jobs).
- `data/sound.sqlite`: sound analysis results, if the analysis runs.
- `data/backups/`: a daily backup at 03:30 server time (`bridge-<date>.sqlite`, `sound-<date>.sqlite`), the
  last 14 days.
- `BACKUP_COPY_DIR` (default `backup-copy/`): the newest backup again (`bridge-latest.sqlite`), ideally on
  another disk.
- `data/tls/`: the status page certificate.
- `chrome-profile/`: the adapter's browser profile.
- `.env` and `config.yaml`: your settings.

**Restoring a backup:** `docker compose stop discovery-bridge`, copy the backup over `data/bridge.sqlite`, delete
`data/bridge.sqlite-wal` and `data/bridge.sqlite-shm` if they exist, then `docker compose up -d`.

**Moving to another server:** stop the containers, copy the repository folder including `.env`, `config.yaml`,
`data/` and `chrome-profile/`, and repeat steps 5 and 6 on the new server.

## 11. Troubleshooting

**"Spotify login expired: renew the sp_dc cookie".** Get a new cookie (step 3), run `scripts/setup.sh` and
answer "n" to keeping `SPOTIFY_SP_DC` (or edit `.env`), then `docker compose up -d --force-recreate`.

**No Discover rows in MA.**

- Is the plugin mounted? `docker exec music-assistant ls <printed directory>/spotify_bridge` should list
  `manifest.json`.
- Is the provider added under Settings > Providers?
- Is `ma_layout_dir: /ma_layout` in `config.yaml`? Without it the covers, genres and rows are not written.
- Does `ma_provider/spotify_bridge/state/layout.json` exist on the server?
- Look for errors from `spotify_bridge` in MA's log. The plugin targets MA 2.10.x; a newer MA can break it.

**A TIDAL or SoundCloud row gets no covers.** `source_rows` matches the start of the row's title as MA shows it,
for example `Custom mixes` for TIDAL. If your rows are named differently, add their titles there. `source_rows: {}`
turns this off.

**Many tracks stay unmatched.** Check that TIDAL is logged in in MA. A track that TIDAL does not carry stays
unmatched; on the status page you can pin the right TIDAL track for a single one.

**The adapter or the analysis does not build.** Both need an x86_64 server. On ARM, remove `feedback` and
`analysis` from `COMPOSE_PROFILES` in `.env` (or run `scripts/setup.sh` and answer no) and empty
`FEEDBACK_API_TOKEN`.

**Feedback jobs fail.** `docker compose logs -f spotify-feedback` shows what the browser saw. The status page can
pause the feedback to Spotify without stopping anything else.

**The status page warns about the certificate.** It is self-signed and expected. The name in it is the
server's host name; set `status_hostname` in `config.yaml` and delete `data/tls/` before a start to change it.
