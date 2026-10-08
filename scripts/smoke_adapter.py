"""Manual smoke test of the adapter API on the home server, run inside the adapter container:
  docker exec spotify-feedback /app/.venv/bin/python /app/scripts/smoke_adapter.py play <track_id> [playlist_id]
  docker exec spotify-feedback /app/.venv/bin/python /app/scripts/smoke_adapter.py save <track_id>
"play" plays the track for 35 s on the configured Spotify account; "save" likes the track (run it twice: the second
run must not click again)."""
import os
import sys
import time

import httpx

URL = os.environ.get("ADAPTER_URL", "http://127.0.0.1:8791")
H = {"Authorization": f"Bearer {os.environ['FEEDBACK_API_TOKEN']}"}


def main() -> None:
    kind, track = sys.argv[1], sys.argv[2]
    playlist = sys.argv[3] if len(sys.argv) > 3 else None
    with httpx.Client(base_url=URL, headers=H, timeout=60) as c:
        print("health", c.get("/health").json())
        print("resume", c.post("/resume").json())
        job_id = int(time.time())
        job = {"id": job_id, "kind": kind, "spotify_id": track, "target_ms": 35_000 if kind == "play" else None,
               "source_spotify_playlist_id": playlist}
        print("submit", c.post("/jobs", json=job).status_code)
        for _ in range(180):
            rec = c.get(f"/jobs/{job_id}").json()
            if rec["status"] == "finished":
                print("result", rec["result"])
                break
            time.sleep(1)
        print("pause", c.post("/pause").json())
        print("token available", c.get("/token").status_code == 200)


main()
