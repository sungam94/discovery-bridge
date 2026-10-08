-- Artists of tracks heard in Music Assistant, added to the MA library (which also follows them on TIDAL or
-- SoundCloud, MA's library sync-back) so their artist pages get the bridge's bio and facts.
CREATE TABLE played_track (ma_item_uri TEXT PRIMARY KEY, checked_at TEXT NOT NULL);
CREATE TABLE played_artist (artist_uri TEXT PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL);
