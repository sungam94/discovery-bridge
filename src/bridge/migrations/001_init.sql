CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO setting(key, value) VALUES ('ingestion_paused', 'false'), ('feedback_paused', 'true');

CREATE TABLE source_track (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  provider_item_id TEXT NOT NULL,
  isrc TEXT,
  artist TEXT NOT NULL,
  title TEXT NOT NULL,
  album TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  explicit INTEGER NOT NULL DEFAULT 0,
  release_date TEXT,
  metadata_fetched INTEGER NOT NULL DEFAULT 0,
  UNIQUE (provider, provider_item_id)
);

CREATE TABLE track_mapping (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
  spotify_id TEXT,
  isrc TEXT,
  ma_provider_uri TEXT,
  ma_library_uri TEXT,
  method TEXT,
  status TEXT NOT NULL CHECK (status IN ('matched', 'unmatched', 'pending', 'invalidated')),
  unmatched_reason TEXT,
  unmatched_retry_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  resolved_at TEXT NOT NULL
);
CREATE UNIQUE INDEX track_mapping_forward ON track_mapping(spotify_id)
  WHERE direction = 'forward' AND status != 'invalidated';
CREATE UNIQUE INDEX track_mapping_reverse ON track_mapping(ma_provider_uri)
  WHERE direction = 'reverse' AND status != 'invalidated';

CREATE TABLE excluded_item (
  spotify_id TEXT NOT NULL,
  ma_provider_uri TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE manual_override (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
  spotify_id TEXT,
  ma_provider_uri TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX manual_override_forward ON manual_override(spotify_id) WHERE direction = 'forward';
CREATE UNIQUE INDEX manual_override_reverse ON manual_override(ma_provider_uri) WHERE direction = 'reverse';

CREATE TABLE playlist (
  slot TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  spotify_id TEXT,
  name TEXT NOT NULL,
  ma_playlist_id TEXT,
  published_items_hash TEXT,
  published_snapshot_id INTEGER,
  publish_blocked_since TEXT,
  publish_blocked_snapshot_id INTEGER,
  history_set_hash TEXT,
  last_checked TEXT,
  last_seen TEXT,
  last_state TEXT,
  last_error TEXT,
  stale INTEGER NOT NULL DEFAULT 0,
  guard_count INTEGER NOT NULL DEFAULT 0,
  guard_hash TEXT,
  empty_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE unslotted_playlist (spotify_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL);

CREATE TABLE playlist_snapshot (
  id INTEGER PRIMARY KEY,
  slot TEXT NOT NULL REFERENCES playlist(slot),
  spotify_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  ordered_hash TEXT NOT NULL,
  set_hash TEXT NOT NULL,
  revision_id TEXT,
  coverage_matched INTEGER,
  coverage_total INTEGER,
  ma_history_name TEXT,
  ma_history_playlist_id TEXT
);

CREATE TABLE playlist_snapshot_item (
  snapshot_id INTEGER NOT NULL REFERENCES playlist_snapshot(id),
  position INTEGER NOT NULL,
  source_track_id INTEGER NOT NULL REFERENCES source_track(id),
  PRIMARY KEY (snapshot_id, position)
);

CREATE TABLE ingestion_health (
  id INTEGER PRIMARY KEY,
  slot TEXT,
  ts TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT
);
