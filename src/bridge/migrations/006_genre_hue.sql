-- Wheel positions (hue, 0-360) of genres that have no fixed place by name and were placed from the data.
-- A placed genre keeps its hue, so cover colours do not drift.
CREATE TABLE genre_hue (
  genre TEXT PRIMARY KEY,
  hue REAL NOT NULL,
  placed_at TEXT NOT NULL
);
