"""Spec §7 + §9: admin status page. One SQLite connection per request (threadpool-safe)."""
from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bridge.db import connect
from bridge.queue.schedule import daypart, started_today
from bridge.repo import get_setting, is_paused, set_setting
from bridge.timeutil import iso, parse_iso, utcnow

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_FAILURES, WINDOW_S = 5, 15 * 60


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def create_app(db_path: Path, password: str, session_secret: str, now: Callable[[], datetime] = utcnow,
               trigger: Callable[[], None] | None = None, warnings: Sequence[str] = (),
               *, tz: str, max_jobs_per_day: int = 50,
               feedback_trigger: Callable[[], None] | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=session_secret, https_only=True,
                       same_site="strict", max_age=12 * 3600)
    failures: dict[str, deque[float]] = defaultdict(deque)
    poke = trigger or (lambda: None)
    wake_queue = feedback_trigger or (lambda: None)

    def token(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    def authed(request: Request) -> bool:
        return request.session.get("auth") is True

    def guard(request: Request, csrf: str):
        if not authed(request):
            return RedirectResponse("/login", status_code=303)
        if not _same(request.session.get("csrf", ""), csrf):
            return HTMLResponse("bad csrf", status_code=403)
        return None

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"csrf": token(request), "error": None})

    @app.post("/login")
    def login(request: Request, password_in: str = Form(alias="password"), csrf: str = Form("")):
        ip = request.client.host if request.client else "?"
        q = failures[ip]
        while q and time.monotonic() - q[0] > WINDOW_S:
            q.popleft()
        if len(q) >= MAX_FAILURES:
            return HTMLResponse("Too many attempts. Try again in 15 minutes.", status_code=429)
        if not _same(request.session.get("csrf", ""), csrf) or not _same(password_in, password):
            q.append(time.monotonic())
            return TEMPLATES.TemplateResponse(request, "login.html", {"csrf": token(request), "error": "Wrong password"},
                                              status_code=401)
        request.session["auth"] = True
        request.session["csrf"] = secrets.token_urlsafe(24)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not authed(request):
            return RedirectResponse("/login", status_code=303)
        conn = connect(db_path)
        playlists = conn.execute(
            "SELECT p.slot, p.name, p.spotify_id, p.last_checked, p.last_state, p.last_error, p.stale, p.guard_count, "
            "p.empty_count, s.coverage_matched, s.coverage_total, s.fetched_at FROM playlist p "
            "LEFT JOIN playlist_snapshot s ON s.id = (SELECT max(id) FROM playlist_snapshot WHERE slot = p.slot) "
            "ORDER BY p.slot").fetchall()
        unmatched = [{**dict(r), "artist": r["artist"].replace("\x1f", ", ")} for r in conn.execute(
            "SELECT m.spotify_id, m.unmatched_reason, m.unmatched_retry_at, t.artist, t.title "
            "FROM track_mapping m JOIN source_track t ON t.provider='spotify' AND t.provider_item_id = m.spotify_id "
            "WHERE m.direction='forward' AND m.status='unmatched' ORDER BY t.artist LIMIT 200")]
        health = conn.execute("SELECT * FROM ingestion_health ORDER BY id DESC LIMIT 20").fetchall()
        unslotted = conn.execute("SELECT * FROM unslotted_playlist").fetchall()
        local = now().astimezone(ZoneInfo(tz))
        since = iso(local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
        taste = conn.execute("SELECT policy_result, count(*) AS n FROM taste_event WHERE started_at >= ? "
                             "GROUP BY policy_result ORDER BY n DESC", (since,)).fetchall()
        unknown_players = [r[0] for r in conn.execute(
            "SELECT DISTINCT player_ids FROM taste_event WHERE policy_result = 'player_unknown' AND started_at >= ?",
            (since,))]
        weekly = conn.execute("SELECT * FROM weekly_metric ORDER BY window_start DESC LIMIT 8").fetchall()
        feedback_paused = is_paused(conn, "feedback_paused")
        adapter = conn.execute("SELECT * FROM adapter_health ORDER BY id DESC LIMIT 1").fetchone()
        jobs_today = conn.execute("SELECT status, count(*) AS n FROM feedback_job WHERE created_at >= ? "
                                  "GROUP BY status ORDER BY status", (since,)).fetchall()
        by_part = [0, 0, 0, 0]
        for r in conn.execute("SELECT original_ts FROM feedback_job WHERE status = 'pending' AND kind = 'play'"):
            by_part[daypart(parse_iso(r["original_ts"]), tz)] += 1
        bad_jobs = conn.execute(
            "SELECT id, kind, spotify_id, status, COALESCE(error, expire_reason, '') AS why, finished_at "
            "FROM feedback_job WHERE status IN ('failed', 'unverified', 'expired') ORDER BY id DESC LIMIT 10").fetchall()
        unresolved = conn.execute(
            "SELECT ma_provider_uri, unmatched_reason, unmatched_retry_at FROM track_mapping "
            "WHERE direction = 'reverse' AND status = 'unmatched' ORDER BY resolved_at DESC LIMIT 100").fetchall()
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "csrf": token(request), "ingestion_paused": is_paused(conn, "ingestion_paused"), "warnings": warnings,
            "ingestion_state": get_setting(conn, "ingestion_state", "unknown"),
            "playlists": playlists, "unmatched": unmatched, "health": health, "unslotted": unslotted,
            "taste": taste, "unknown_players": unknown_players, "weekly": weekly, "unresolved": unresolved,
            "feedback_paused": feedback_paused, "adapter": adapter, "jobs_today": jobs_today, "by_part": by_part,
            "bad_jobs": bad_jobs, "cap_used": started_today(conn, now(), tz), "cap": max_jobs_per_day})

    @app.post("/settings/ingestion_paused")
    def toggle(request: Request, csrf: str = Form("")):
        if (bad := guard(request, csrf)) is not None:
            return bad
        conn = connect(db_path)
        set_setting(conn, "ingestion_paused", "false" if is_paused(conn, "ingestion_paused") else "true")
        poke()
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/feedback_paused")
    def toggle_feedback(request: Request, csrf: str = Form("")):
        if (bad := guard(request, csrf)) is not None:
            return bad
        conn = connect(db_path)
        set_setting(conn, "feedback_paused", "false" if is_paused(conn, "feedback_paused") else "true")
        wake_queue()
        return RedirectResponse("/", status_code=303)

    @app.post("/overrides")
    def override(request: Request, csrf: str = Form(""), spotify_id: str = Form(...),
                 ma_provider_uri: str = Form(""), block: str = Form("")):
        if (bad := guard(request, csrf)) is not None:
            return bad
        blocked = block == "1"
        uri = None if blocked else ma_provider_uri.strip()
        if not blocked and not (uri or "").startswith("tidal"):
            return HTMLResponse("A pin needs a TIDAL provider URI (tidal--…://track/…).", status_code=400)
        conn = connect(db_path)
        sid = spotify_id.strip()
        conn.execute("DELETE FROM manual_override WHERE direction='forward' AND spotify_id=?", (sid,))
        conn.execute("INSERT INTO manual_override(direction, spotify_id, ma_provider_uri, blocked, created_at) "
                     "VALUES ('forward', ?, ?, ?, ?)", (sid, uri, int(blocked), iso(now())))
        conn.execute("UPDATE track_mapping SET status='invalidated' WHERE direction='forward' AND spotify_id=? "
                     "AND status!='invalidated'", (sid,))
        poke()
        return RedirectResponse("/", status_code=303)

    @app.post("/overrides/reverse")
    def reverse_override(request: Request, csrf: str = Form(""), ma_provider_uri: str = Form(...),
                         spotify_id: str = Form(""), block: str = Form("")):
        if (bad := guard(request, csrf)) is not None:
            return bad
        blocked = block == "1"
        sid = None if blocked else spotify_id.strip()
        if not blocked and not sid:
            return HTMLResponse("A pin needs a Spotify track ID.", status_code=400)
        conn = connect(db_path)
        uri = ma_provider_uri.strip()
        conn.execute("DELETE FROM manual_override WHERE direction='reverse' AND ma_provider_uri=?", (uri,))
        conn.execute("INSERT INTO manual_override(direction, spotify_id, ma_provider_uri, blocked, created_at) "
                     "VALUES ('reverse', ?, ?, ?, ?)", (sid, uri, int(blocked), iso(now())))
        conn.execute("UPDATE track_mapping SET status='invalidated' WHERE direction='reverse' AND ma_provider_uri=? "
                     "AND status!='invalidated'", (uri,))
        provider, item_id = uri.split("://", 1)[0], uri.rsplit("/", 1)[1]
        conn.execute("UPDATE taste_event SET policy_result='pending_resolve' WHERE policy_result='unresolved' "
                     "AND provider=? AND provider_item_id=?", (provider, item_id))
        return RedirectResponse("/", status_code=303)

    return app
