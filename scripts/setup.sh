#!/usr/bin/env bash
# First setup of Discovery Bridge on the home server. Run it from anywhere; it works in the repository folder.
#
#   scripts/setup.sh           create .env and config.yaml from the examples if they are missing, then ask for
#                              the values a new installation needs
#   scripts/setup.sh --check   report which settings are filled in (never their values), whether Music
#                              Assistant answers and whether the plugin folder is ready
#
# Every question shows the current value as its default (secrets only as "set"), so pressing Enter keeps it and
# running the script again changes nothing. In existing files only the asked values change; every other line
# stays. Secrets are read without echo and never printed. .env is kept at mode 600.
#
# Without a terminal, SETUP_NONINTERACTIVE=1 takes the answers from the environment instead:
#   SETUP_MA_TOKEN, SETUP_SPOTIFY_SP_DC, SETUP_STATUS_PASSWORD, SETUP_LASTFM_API_KEY (secrets; a value already in
#   .env is kept when the variable is not set), SETUP_TZ, SETUP_FEEDBACK and SETUP_ANALYSIS (yes or no),
#   SETUP_MA_URL (host, host:port or a ws:// address), SETUP_LANGUAGE (en or de), SETUP_PLAYERS (comma separated
#   MA player ids). A variable that is not set keeps the current value.
# SETUP_SKIP_NETWORK=1 makes --check leave out the request to Music Assistant.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.."

ENV_FILE=.env
CONFIG_FILE=config.yaml
STATE_DIR=ma_provider/spotify_bridge/state
NONINTERACTIVE="${SETUP_NONINTERACTIVE:-}"

say() { printf '%s\n' "$*"; }
die() { printf 'setup: %s\n' "$*" >&2; exit 1; }

# --- reading and writing the two files ------------------------------------------------------------------------
# Values travel through the environment of awk (ENVIRON), never through its arguments, so they do not show up in
# the process list and backslashes stay as they are.

env_get() {  # env_get KEY: the value of KEY in .env (for the script's own use, never shown)
  SETUP_KEY="$1" awk 'BEGIN { k = ENVIRON["SETUP_KEY"] "=" }
    index($0, k) == 1 { v = substr($0, length(k) + 1) } END { printf "%s", v }' "$ENV_FILE"
}

env_set() {  # env_set KEY VALUE: replaces the KEY= line of .env, or appends one
  local tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  SETUP_KEY="$1" SETUP_VAL="$2" awk 'BEGIN { k = ENVIRON["SETUP_KEY"]; v = ENVIRON["SETUP_VAL"] }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

yaml_get() {  # yaml_get KEY: the value of a top-level KEY in config.yaml (empty when missing or commented out)
  SETUP_KEY="$1" awk 'BEGIN { k = ENVIRON["SETUP_KEY"] ":" }
    index($0, k) == 1 { v = substr($0, length(k) + 1); sub(/^[ \t]+/, "", v); sub(/[ \t]+$/, "", v) }
    END { printf "%s", v }' "$CONFIG_FILE"
}

yaml_set() {  # yaml_set KEY VALUE: replaces the first "KEY:" or "# KEY:" line of config.yaml, or appends one
  local tmp
  tmp="$(mktemp "$CONFIG_FILE.XXXXXX")"
  SETUP_KEY="$1" SETUP_VAL="$2" awk 'BEGIN { k = ENVIRON["SETUP_KEY"]; v = ENVIRON["SETUP_VAL"] }
    !done && (index($0, k ":") == 1 || index($0, "# " k ":") == 1) { print k ": " v; done = 1; next }
    { print }
    END { if (!done) print k ": " v }' "$CONFIG_FILE" > "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$CONFIG_FILE"
}

random_hex() {  # 32 random bytes as 64 hex characters
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    printf '\n'
  fi
}

# --- questions -------------------------------------------------------------------------------------------------
# Each sets the global "answer".

from_env() {  # from_env NAME: true when SETUP_NAME is set; its value goes to "answer"
  local value
  if value="$(printenv "SETUP_$1")"; then
    answer="$value"
    return 0
  fi
  return 1
}

ask() {  # ask NAME QUESTION DEFAULT
  answer=""
  if [ -n "$NONINTERACTIVE" ]; then
    from_env "$1" || answer="$3"
    return 0
  fi
  read -r -p "$2 [$3]: " answer || true
  answer="${answer:-$3}"
}

ask_yes_no() {  # ask_yes_no NAME QUESTION DEFAULT(yes|no); answer becomes yes or no
  local default="$3" reply
  while true; do
    if [ -n "$NONINTERACTIVE" ]; then
      from_env "$1" || answer="$default"
      reply="$answer"
    else
      read -r -p "$2 [$( [ "$default" = yes ] && echo Y/n || echo y/N )]: " reply || true
      reply="${reply:-$default}"
    fi
    case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
      y|yes|1|true) answer=yes; return 0 ;;
      n|no|0|false) answer=no; return 0 ;;
    esac
    [ -z "$NONINTERACTIVE" ] || die "SETUP_$1 must be yes or no"
    say "Please answer yes or no."
  done
}

valid_secret() {  # characters that .env files and Docker Compose would read differently are refused
  case "$1" in
    *[[:space:]\"\'\\\$]*) return 1 ;;
  esac
  return 0
}

ask_secret() {  # ask_secret KEY QUESTION required|optional; writes .env, prints only the key name
  local key="$1" question="$2" need="$3" current value again
  current="$(env_get "$key")"
  if [ -n "$NONINTERACTIVE" ]; then
    if from_env "$key" && [ -n "$answer" ]; then
      value="$answer"
    else
      value="$current"
    fi
    [ -n "$value" ] || [ "$need" = optional ] || die "$key is empty: set SETUP_$key"
    valid_secret "$value" || die "SETUP_$key contains spaces, quotes, a backslash or a dollar sign"
  else
    if [ -n "$current" ]; then
      ask_yes_no "KEEP_$key" "$key is set. Keep it?" yes
      if [ "$answer" = yes ]; then
        say "  $key kept"
        return 0
      fi
    fi
    while true; do
      if ! read -r -s -p "$question: " value; then
        printf '\n'
        die "the input ended before $key was entered"
      fi
      printf '\n'
      if [ -z "$value" ]; then
        [ "$need" = optional ] && break
        say "  $key is required."
        continue
      fi
      if ! valid_secret "$value"; then
        say "  Spaces, quotes, backslashes and dollar signs do not work in .env; please use another value."
        continue
      fi
      if [ "$key" = STATUS_PASSWORD ]; then
        if ! read -r -s -p "Repeat the password: " again; then
          printf '\n'
          die "the input ended before the password was repeated"
        fi
        printf '\n'
        if [ "$again" != "$value" ]; then
          say "  The two entries differ."
          continue
        fi
      fi
      break
    done
  fi
  env_set "$key" "$value"
  if [ -n "$value" ]; then say "  $key set"; else say "  $key left empty"; fi
}

detect_timezone() {
  local tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  fi
  if [ -z "$tz" ] && [ -r /etc/timezone ]; then
    tz="$(head -n 1 /etc/timezone)"
  fi
  if [ -z "$tz" ] && [ -L /etc/localtime ]; then
    tz="$(readlink /etc/localtime | sed 's|.*zoneinfo/||')"
  fi
  printf '%s' "${tz:-UTC}"
}

valid_timezone() {  # checked against the system's zone files when it has them
  [ -n "$1" ] || return 1
  case "$1" in *[[:space:]]*|/*|*..*) return 1 ;; esac
  if [ -d /usr/share/zoneinfo ]; then
    [ -f "/usr/share/zoneinfo/$1" ] || return 1
  fi
  return 0
}

ws_url() {  # host, host:port, or an http(s):// or ws(s):// address -> the MA WebSocket address
  local url="$1"
  case "$url" in
    http://*) url="ws://${url#http://}" ;;
    https://*) url="wss://${url#https://}" ;;
    ws://*|wss://*) ;;
    *:*) url="ws://$url" ;;
    *) url="ws://$url:8095" ;;
  esac
  url="${url%/}"
  case "$url" in */ws) ;; *) url="$url/ws" ;; esac
  printf '%s' "$url"
}

players_yaml() {  # "a, b" -> ["a", "b"]; quoted, so an id shaped like a MAC address stays a string
  local out="" id
  local IFS=,
  for id in $1; do
    id="$(printf '%s' "$id" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -n "$id" ] || continue
    case "$id" in *[\"\\]*) return 1 ;; esac
    out="${out:+$out, }\"$id\""
  done
  printf '[%s]' "$out"
}

has_profile() {  # has_profile NAME: true when COMPOSE_PROFILES in .env names it
  case ",$(env_get COMPOSE_PROFILES | tr -d ' ')," in *",$1,"*) return 0 ;; esac
  return 1
}

TERMS_NOTE='
The feedback adapter plays tracks and saves likes on your Spotify account, in a real Chrome browser, so that
Spotify learns from what you heard in Music Assistant. Spotify'"'"'s terms forbid automated use of the service,
and automated playback is the kind of activity Spotify looks for: the risk is a suspended or closed account.
Use an account you can afford to lose and keep max_jobs_per_day low. Everything else works without the adapter.
(See "Spotify terms" in README.md.)
'

# --- --check -----------------------------------------------------------------------------------------------------

check() {
  local problems=0 key n url http mode
  if [ ! -f "$ENV_FILE" ]; then
    say ".env: missing (run scripts/setup.sh)"
    problems=1
  else
    mode="$(ls -l "$ENV_FILE" | cut -c1-10)"
    [ "$mode" = "-rw-------" ] || say ".env: readable by others ($mode); run: chmod 600 .env"
    for key in MA_TOKEN SPOTIFY_SP_DC STATUS_PASSWORD SESSION_SECRET; do
      n="$(grep -c "^$key=..*" "$ENV_FILE" || true)"
      if [ "$n" -gt 0 ]; then say "$key: set"; else say "$key: MISSING"; problems=1; fi
    done
    for key in FEEDBACK_API_TOKEN LASTFM_API_KEY; do
      n="$(grep -c "^$key=..*" "$ENV_FILE" || true)"
      if [ "$n" -gt 0 ]; then say "$key: set"; else say "$key: empty (optional)"; fi
    done
    say "COMPOSE_PROFILES: $(env_get COMPOSE_PROFILES)"
    say "TZ: $(env_get TZ)"
    if has_profile feedback && [ -z "$(env_get FEEDBACK_API_TOKEN)" ]; then
      say "The feedback profile is on but FEEDBACK_API_TOKEN is empty: the adapter will not start."
      problems=1
    fi
    if ! has_profile feedback && [ -n "$(env_get FEEDBACK_API_TOKEN)" ]; then
      say "FEEDBACK_API_TOKEN is set but the feedback profile is off: the bridge will queue jobs nobody runs."
    fi
  fi
  if [ ! -f "$CONFIG_FILE" ]; then
    say "config.yaml: missing (run scripts/setup.sh)"
    problems=1
  else
    say "language: $(yaml_get language)"
    if [ "$(yaml_get players_allowlist)" = "[]" ] || [ -z "$(yaml_get players_allowlist)" ]; then
      say "players_allowlist: empty, so no plays count as taste yet"
    else
      say "players_allowlist: set"
    fi
    url="$(yaml_get ma_url)"
    say "ma_url: $url"
    if [ -n "${SETUP_SKIP_NETWORK:-}" ]; then
      say "Music Assistant: not checked (SETUP_SKIP_NETWORK)"
    elif ! command -v curl >/dev/null 2>&1; then
      say "Music Assistant: not checked (curl is not installed)"
    else
      http="$(printf '%s' "$url" | sed 's|^ws://|http://|; s|^wss://|https://|; s|/ws$|/|')"
      if curl -s -k -o /dev/null --max-time 5 "$http"; then
        say "Music Assistant: answers at $http"
      else
        say "Music Assistant: no answer at $http"
        problems=1
      fi
    fi
  fi
  if [ -d "$STATE_DIR" ]; then
    say "Plugin state folder: $STATE_DIR exists"
  else
    say "Plugin state folder: $STATE_DIR is missing (run scripts/setup.sh)"
    problems=1
  fi
  return "$problems"
}

# --- setup -------------------------------------------------------------------------------------------------------

setup() {
  local new_env=0 tz feedback analysis profiles url language players current_players

  [ -f .env.example ] && [ -f config.example.yaml ] || die "run this from a copy of the Discovery Bridge repository"

  if [ ! -f "$ENV_FILE" ]; then
    cp .env.example "$ENV_FILE"
    new_env=1
    say "Created .env from .env.example."
  fi
  chmod 600 "$ENV_FILE"
  if [ ! -f "$CONFIG_FILE" ]; then
    cp config.example.yaml "$CONFIG_FILE"
    chmod 644 "$CONFIG_FILE"
    say "Created config.yaml from config.example.yaml."
  fi

  say ""
  say "Secrets (stored in .env, never shown):"
  ask_secret MA_TOKEN "Music Assistant long-lived token (MA > Settings > Profile > Long-lived tokens)" required
  ask_secret SPOTIFY_SP_DC "Value of the sp_dc cookie of open.spotify.com (see docs/SETUP.md)" required
  ask_secret STATUS_PASSWORD "Password for the status page" required
  ask_secret LASTFM_API_KEY "Last.fm API key for artist biographies (optional, Enter skips)" optional
  if [ -z "$(env_get SESSION_SECRET)" ]; then
    env_set SESSION_SECRET "$(random_hex)"
    say "  SESSION_SECRET generated"
  else
    say "  SESSION_SECRET kept"
  fi

  say ""
  say "Server:"
  if [ "$new_env" = 1 ]; then tz="$(detect_timezone)"; else tz="$(env_get TZ)"; fi
  while true; do
    ask TZ "Timezone of the server (IANA name such as Europe/London)" "${tz:-UTC}"
    valid_timezone "$answer" && break
    [ -z "$NONINTERACTIVE" ] || die "SETUP_TZ is not a known timezone: $answer"
    say "  Unknown timezone."
  done
  env_set TZ "$answer"

  say "$TERMS_NOTE"
  ask_yes_no FEEDBACK "Run the feedback adapter (plays on your Spotify account)?" \
    "$(has_profile feedback && echo yes || echo no)"
  feedback="$answer"
  ask_yes_no ANALYSIS "Run the sound analysis (moods on the covers, needs an x86_64 host)?" \
    "$(has_profile analysis && echo yes || echo no)"
  analysis="$answer"
  case "$(uname -m)" in
    x86_64|amd64) ;;
    *) if [ "$feedback" = yes ] || [ "$analysis" = yes ]; then
         say "  Note: this machine is $(uname -m); the adapter and the sound analysis images build on x86_64 only."
       fi ;;
  esac
  profiles=""
  [ "$feedback" = yes ] && profiles="feedback"
  [ "$analysis" = yes ] && profiles="${profiles:+$profiles,}analysis"
  env_set COMPOSE_PROFILES "$profiles"
  if [ "$feedback" = yes ]; then
    if [ -z "$(env_get FEEDBACK_API_TOKEN)" ]; then
      env_set FEEDBACK_API_TOKEN "$(random_hex)"
      say "  FEEDBACK_API_TOKEN generated"
    fi
  else
    # an empty token keeps the bridge from queueing jobs for an adapter that does not run
    env_set FEEDBACK_API_TOKEN ""
  fi
  say "  COMPOSE_PROFILES=${profiles}"

  say ""
  say "Music Assistant (config.yaml):"
  url="$(yaml_get ma_url)"
  ask MA_URL "Music Assistant address: host, host:port, or the http:// or ws:// address (127.0.0.1 when MA runs on this server)" \
    "${url:-ws://127.0.0.1:8095/ws}"
  url="$(ws_url "$answer")"
  yaml_set ma_url "$url"
  say "  ma_url: $url"

  language="$(yaml_get language)"
  while true; do
    ask LANGUAGE "Language of the Discover row titles and the health texts (en or de)" "${language:-en}"
    case "$answer" in en|de) break ;; esac
    [ -z "$NONINTERACTIVE" ] || die "SETUP_LANGUAGE must be en or de"
    say "  Please answer en or de."
  done
  yaml_set language "$answer"
  say "  language: $answer"

  current_players="$(yaml_get players_allowlist)"
  if [ -n "$NONINTERACTIVE" ]; then
    if from_env PLAYERS; then players="$answer"; else players="__keep__"; fi
  else
    say "MA players whose plays count as your taste: the id is the last part of a player's page address in MA"
    say "(Settings > Players > open the player). You can add them later in config.yaml."
    read -r -p "Player ids, comma separated (Enter keeps ${current_players:-[]}): " players || true
    players="${players:-__keep__}"
  fi
  if [ "$players" != "__keep__" ]; then
    players="$(players_yaml "$players")" || die "player ids must not contain quotes or backslashes"
    yaml_set players_allowlist "$players"
  fi
  say "  players_allowlist: $(yaml_get players_allowlist)"

  make_folders

  say ""
  say "Done. Next steps (docs/SETUP.md has the details):"
  say "  1. Mount the plugin into your Music Assistant container, read-only:"
  say "       $(pwd)/ma_provider/spotify_bridge:<MA providers directory>/spotify_bridge:ro"
  say "     then restart MA and enable \"Spotify Bridge\" under Settings > Providers."
  say "  2. docker compose up -d --build"
  say "  3. scripts/setup.sh --check"
}

make_folders() {  # created now, so Docker does not create them owned by root
  local dir
  for dir in data chrome-profile "$STATE_DIR" "$(env_get BACKUP_COPY_DIR)" "$(env_get HEALTH_DIR)"; do
    [ -n "$dir" ] || continue
    if ! mkdir -p "$dir" 2>/dev/null; then
      say "  Could not create $dir; create it yourself before starting the containers."
    fi
  done
  say "  Folders ready: data, chrome-profile, $STATE_DIR, $(env_get BACKUP_COPY_DIR), $(env_get HEALTH_DIR)"
}

case "${1:-}" in
  --check) check ;;
  "") setup ;;
  -h|--help) sed -n '2,18p' "$SELF" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown option $1 (use --check or no option)" ;;
esac
