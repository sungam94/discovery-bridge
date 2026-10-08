-- Playlists copied generically from configured Made For You hub sections.
ALTER TABLE playlist ADD COLUMN section TEXT;
ALTER TABLE playlist ADD COLUMN hub_position INTEGER;
