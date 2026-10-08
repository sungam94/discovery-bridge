-- Facts for Music Assistant artist pages: the raw parts from Last.fm, MusicBrainz and Wikipedia (fetched_at says
-- when), and the text last handed to MA (recomposed whenever the local facts change).
CREATE TABLE artist_info (
  artist_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parts TEXT NOT NULL,
  text TEXT,
  fetched_at TEXT NOT NULL
);
