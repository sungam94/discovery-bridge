# Discovery Bridge

Discovery Bridge brings the personal playlists of a Spotify Free account into
[Music Assistant](https://www.music-assistant.io/) (MA), where they play from TIDAL. Discover Weekly, Release
Radar, the daylist, the Daily Mixes and the mixes of Spotify's "Made for you" page show up in MA as playlists
named `Spotify · …`, with their own Discover rows and covers. What you then listen to in MA can be played back
on Spotify, so that Spotify's recommendations keep following your taste.

It is a hobby project for people who already run a home server with Docker, Music Assistant, a Spotify Free
account and a TIDAL subscription. Everything runs on your own server, with your own accounts.

## Spotify terms

Read this before you install anything.

- Spotify's terms forbid automated access to the service and anything that artificially inflates play counts.
- **The bridge** reads the web player's private API with your login cookie. It is personal and read-only, and
  it reads about once an hour with a pause between requests. It is still against the terms.
- **The feedback adapter** goes further. It drives a real Chrome browser that plays tracks and saves likes on
  your Spotify account, to reproduce what you heard in MA. That is automated playback, the kind of activity
  Spotify looks for. The risk is a suspended or closed account.
- Use an account you can afford to lose, keep the default limit (`max_jobs_per_day: 50` in `config.yaml`), and
  never point the bridge at someone else's account.
- **Running without the adapter:** leave `feedback` out of `COMPOSE_PROFILES` in `.env` and leave
  `FEEDBACK_API_TOKEN` empty (this is the default). The bridge still mirrors the playlists, draws the covers and
  records your plays locally; nothing is played on Spotify. There is no mode without the cookie, because the
  personal playlists are only reachable when logged in.

## What it does

- **Mirrors the playlists.** Once an hour the bridge reads your "Made for you" page on Spotify, matches every
  track to TIDAL through MA (a search by artist and title that only accepts a track with the same ISRC and
  length), and writes the result into MA's library as
  `Spotify · Discover Weekly`, `Spotify · Release Radar`, `Spotify · Daily Mix 1` and so on. Discover Weekly and
  Release Radar also get a dated copy whenever their tracks change (`Spotify · DW 2026-09-28`).
- **Discover rows and covers in MA.** A small MA plugin (`ma_provider/spotify_bridge`) adds Discover rows for
  these playlists and draws covers with the playlist's genres and mood. Optionally it gives TIDAL and SoundCloud
  mixes the same covers (`source_rows` in `config.yaml`). Artist pages in MA get a short biography from
  MusicBrainz, Wikipedia and, with an API key, Last.fm.
- **Records your listening.** Plays on the MA players you choose count as taste: completed, listened, skipped,
  and favourites.
- **Feeds it back to Spotify (optional).** The feedback adapter replays completed and listened plays, and saves
  favourites as likes, on Spotify. It uses a Chrome browser on a virtual display inside its container, does at
  most `max_jobs_per_day` a day, and plays each track in the same hour of the day as you heard it. It does not
  start a track while another of your devices is playing on Spotify.
- **Analyses the sound (optional).** The sound analysis service rates mood and voice or instrumental from
  Deezer's 30-second previews with the Essentia models. The results appear as a mood line on the covers.
- **Status page and health file.** A password-protected status page shows the playlists, unmatched tracks
  (with a way to pin the right TIDAL track) and the feedback queue. A `health.json` file lists current problems
  for any watchdog.

<!-- Screenshots, to be added: docs/images/discover-rows.png, docs/images/covers.png, docs/images/status-page.png -->

## How it fits together

```
Spotify web player --hub and playlists, read with your cookie--> discovery-bridge --TIDAL playlists--> Music Assistant
Music Assistant --play events--> discovery-bridge --feedback jobs--> spotify-feedback --plays, likes--> Spotify
discovery-bridge --layout.json, artist_info.json--> spotify_bridge plugin in MA --> Discover rows and covers
data/bridge.sqlite --> sound-analysis --> data/sound.sqlite --> discovery-bridge (moods on the covers)
```

| Part | Where | Runs |
|---|---|---|
| `discovery-bridge` | `src/bridge`, `Dockerfile` | always; host networking, status page on port 8790 |
| `spotify-feedback` | `src/adapter`, `adapter/Dockerfile` | with the compose profile `feedback`; x86_64 only |
| `sound-analysis` | `src/analysis`, `analysis/Dockerfile` | with the compose profile `analysis`; x86_64 only |
| `spotify_bridge` MA plugin | `ma_provider/spotify_bridge` | inside your MA container, mounted read-only |

The bridge and the plugin talk only through files: the bridge writes `layout.json` and `artist_info.json` into
`ma_provider/spotify_bridge/state/`, and MA reads them through its own mount of the plugin folder. That is why
MA and the bridge must run on the same host.

## Getting started

[docs/SETUP.md](docs/SETUP.md) walks through the whole installation. In short, on the server:

```
scripts/setup.sh            # creates .env and config.yaml and asks for the values
# mount ma_provider/spotify_bridge into your MA container (docs/SETUP.md, step 5)
docker compose up -d --build
scripts/setup.sh --check
```

All settings are described in [config.example.yaml](config.example.yaml) (behaviour) and
[.env.example](.env.example) (secrets and server values).

## Music Assistant version

The plugin and the bridge were built against Music Assistant 2.10.x (2.10.4, API schema 65). The plugin hooks
into some MA internals, so a newer MA version can break it. If the Discover rows or covers disappear after an
MA update, check MA's log for errors from `spotify_bridge`.

## Development

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```
uv sync
uv run pytest -q
```

The tests need no network and no accounts. `uv run python -m bridge.cli publish-once discover_weekly` ingests
and publishes one playlist once, using `config.yaml` and `.env` in the current folder; it talks to your Spotify
account and your MA.

## Licences

- The project's own code: MIT (see `LICENSE`).
- Fonts in `ma_provider/spotify_bridge/fonts/`: Archivo Black and DM Mono, both under the SIL Open Font Licence
  (`OFL-ArchivoBlack.txt`, `OFL-DMMono.txt`).
- The sound analysis downloads the Essentia TensorFlow models of the Music Technology Group (Universitat Pompeu
  Fabra) at build time. They are licensed CC BY-NC-SA 4.0: non-commercial use only.
- Spotify, TIDAL, SoundCloud and Music Assistant are not affiliated with this project.
