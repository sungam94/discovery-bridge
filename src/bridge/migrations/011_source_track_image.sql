-- the album cover of a source playlist's track as MA lists it, JSON [path, provider]; NULL = none listed.
-- TIDAL covers show the first six different ones.
ALTER TABLE source_playlist_track ADD COLUMN image TEXT;
