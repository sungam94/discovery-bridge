-- TIDAL and SoundCloud playlists from Music Assistant's Discover rows (config source_rows) and their tracks, for
-- the genres and moods on their covers. A playlist that leaves its row is deleted with its tracks.
CREATE TABLE source_playlist (
  uri TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  row_name TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
-- artists joined by the unit separator; genres = the uploader's genre tags (SoundCloud), as a JSON list
CREATE TABLE source_playlist_track (
  playlist_uri TEXT NOT NULL REFERENCES source_playlist(uri) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  track_uri TEXT NOT NULL,
  artists TEXT NOT NULL,
  isrc TEXT,
  genres TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (playlist_uri, position)
);
CREATE INDEX source_playlist_track_isrc ON source_playlist_track(isrc);
-- ISRC of a TIDAL track (the playlist listing has none, the full item has); NULL = the full item had none.
-- Asked once per track uri.
CREATE TABLE source_track_isrc (track_uri TEXT PRIMARY KEY, isrc TEXT, fetched_at TEXT NOT NULL);
