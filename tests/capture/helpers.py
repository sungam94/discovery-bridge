"""Seed Phase 1 tables for capture tests."""


def seed_snapshot(conn, slot, ma_playlist_id, tracks, *, set_hash="s",
                  fetched_at="2026-10-05T00:00:00+00:00", kind="fixed", spotify_playlist_id=None):
    sp_pid = spotify_playlist_id or f"sp_{slot}"
    conn.execute("INSERT OR IGNORE INTO playlist(slot, kind, spotify_id, name) VALUES (?, ?, ?, ?)",
                 (slot, kind, sp_pid, f"Spotify · {slot}"))
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash, "
                       "coverage_matched, coverage_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (slot, sp_pid, fetched_at, f"o{set_hash}{fetched_at}", set_hash,
                        sum(1 for _, u in tracks if u), len(tracks))).lastrowid
    for pos, (spotify_id, ma_uri) in enumerate(tracks):
        conn.execute("INSERT OR IGNORE INTO source_track(provider, provider_item_id, artist, title, album, "
                     "duration_ms) VALUES ('spotify', ?, 'a', 't', 'al', 240000)", (spotify_id,))
        tid = conn.execute("SELECT id FROM source_track WHERE provider_item_id = ?", (spotify_id,)).fetchone()[0]
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
        if ma_uri and not conn.execute("SELECT 1 FROM track_mapping WHERE direction='forward' AND spotify_id=?",
                                       (spotify_id,)).fetchone():
            conn.execute("INSERT INTO track_mapping(direction, spotify_id, isrc, ma_provider_uri, method, status, "
                         "resolved_at) VALUES ('forward', ?, ?, ?, 'catalog_isrc', 'matched', ?)",
                         (spotify_id, f"ISRC_{spotify_id}", ma_uri, fetched_at))
    conn.execute("UPDATE playlist SET ma_playlist_id = ?, published_snapshot_id = ? WHERE slot = ?",
                 (ma_playlist_id, sid, slot))
    return sid
