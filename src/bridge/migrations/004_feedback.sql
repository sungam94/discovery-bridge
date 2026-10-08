-- Phase 3: feedback jobs (spec §6, §8 as amended by the Phase 3 spike and plan; recent_play is not created).
CREATE TABLE feedback_job (
  id INTEGER PRIMARY KEY,
  taste_event_id INTEGER UNIQUE REFERENCES taste_event(id),
  kind TEXT NOT NULL CHECK (kind IN ('play', 'save')),
  spotify_id TEXT NOT NULL,
  observed_spotify_id TEXT,
  target_ms INTEGER,
  source_spotify_playlist_id TEXT,
  original_ts TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'verified', 'unverified', 'failed', 'expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  error TEXT,
  expire_reason TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX feedback_job_status ON feedback_job(status);

-- One row per job start (retries included): the daily cap counts these.
CREATE TABLE job_start (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES feedback_job(id),
  ts TEXT NOT NULL
);
CREATE INDEX job_start_ts ON job_start(ts);

CREATE TABLE adapter_health (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT
);
