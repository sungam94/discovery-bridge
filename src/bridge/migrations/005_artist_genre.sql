-- Genres per artist from MusicBrainz, for the genre frame on playlist covers.
-- A row with an empty list means "looked up, nothing found", so the artist is not asked again until the row is old.
CREATE TABLE artist_genre (
  artist_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  mbid TEXT,
  genres TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
