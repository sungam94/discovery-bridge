-- Phase 2: capture (spec §5, §8). open_play.media_type is an addition to §8, needed by filter 2 (§5.3).
CREATE TABLE open_play (
  player_id TEXT PRIMARY KEY,
  ma_item_uri TEXT NOT NULL,
  media_type TEXT,
  userid TEXT,
  duration_ms INTEGER,
  started_at TEXT NOT NULL,
  seconds_played REAL NOT NULL DEFAULT 0,
  fully_played INTEGER NOT NULL DEFAULT 0,
  queue_error INTEGER NOT NULL DEFAULT 0,
  source_ma_playlist_id TEXT,
  source_radio INTEGER NOT NULL DEFAULT 0,
  queue_id TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE pending_favorite (
  ma_item_uri TEXT PRIMARY KEY,
  flagged_at TEXT NOT NULL,
  release_at TEXT NOT NULL
);

CREATE TABLE taste_event (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  ma_item_uri TEXT NOT NULL,
  provider TEXT,
  provider_item_id TEXT NOT NULL,
  isrc TEXT,
  queue_id TEXT,
  player_ids TEXT,
  userid TEXT,
  started_at TEXT NOT NULL,
  listened_ms INTEGER,
  duration_ms INTEGER,
  outcome TEXT NOT NULL,
  spotify_id TEXT,
  source_ma_playlist_id TEXT,
  source_spotify_playlist_id TEXT,
  source_snapshot_id INTEGER,
  source_radio INTEGER NOT NULL DEFAULT 0,
  policy_result TEXT NOT NULL,
  UNIQUE(source, provider_item_id, started_at)
);
CREATE INDEX taste_event_started ON taste_event(started_at);
CREATE INDEX taste_event_policy ON taste_event(policy_result);

CREATE TABLE weekly_metric (
  snapshot_id INTEGER PRIMARY KEY REFERENCES playlist_snapshot(id),
  window_start TEXT NOT NULL,
  window_end TEXT,
  matched INTEGER NOT NULL,
  played INTEGER NOT NULL,
  completed INTEGER NOT NULL,
  favorited INTEGER NOT NULL,
  feedback_days INTEGER
);
