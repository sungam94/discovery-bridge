-- The full answers of Last.fm and MusicBrainz per artist, kept for later use (the parts only hold what the
-- artist page shows). Rows from before this column are fetched again.
ALTER TABLE artist_info ADD COLUMN raw TEXT;
