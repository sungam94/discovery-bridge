"""Bridge database rows for the sound analysis tests."""
from bridge.db import connect, migrate


def bridge_db(path):
    c = connect(path)
    migrate(c)
    return c


def playlist(conn, slot, name, isrcs, published=True, stale=0, live=True):
    """One track per ISRC (None for a track without one); live=False leaves the playlist without an MA id."""
    conn.execute("INSERT INTO playlist(slot, kind, name, ma_playlist_id, stale) VALUES (?, 'fixed', ?, ?, ?)",
                 (slot, name, "7" if live else None, stale))
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                       "VALUES (?, 'x', 't', 'o', 's')", (slot,)).lastrowid
    for pos, isrc in enumerate(isrcs):
        row = conn.execute("SELECT id FROM source_track WHERE isrc = ?", (isrc,)).fetchone() if isrc else None
        tid = row["id"] if row else conn.execute(
            "INSERT INTO source_track(provider, provider_item_id, isrc, artist, title, album, duration_ms) "
            "VALUES ('spotify', ?, ?, 'a', 't', 'al', 1000)", (f"{slot}{pos}", isrc)).lastrowid
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
    if published:
        conn.execute("UPDATE playlist SET published_snapshot_id = ? WHERE slot = ?", (sid, slot))


def source_playlist(conn, uri, kind, isrcs, name="My Mix 1"):
    """A TIDAL or SoundCloud Discover playlist, one track per ISRC (None for a track without one)."""
    conn.execute("INSERT INTO source_playlist(uri, name, source, row_name, fetched_at) VALUES (?, ?, ?, 'r', 't')",
                 (uri, name, kind))
    for pos, isrc in enumerate(isrcs):
        conn.execute("INSERT INTO source_playlist_track(playlist_uri, position, track_uri, artists, isrc) "
                     "VALUES (?, ?, ?, 'a', ?)", (uri, pos, f"{uri}/t{pos}", isrc))
