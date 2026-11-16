"""
FastAPI app of the GUI. Local only: it must be bound to 127.0.0.1, rejects other Host headers
(DNS rebinding) and requires the per-launch token on every request that changes something.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from greeceapt.gui import queries, review
from greeceapt.gui.actions import ACTIONS, resolve_params, validate_params
from greeceapt.gui.jobs import JobManager

STATIC = Path(__file__).with_name("static")
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def ollama_status() -> dict[str, Any]:
    try:
        response = httpx.get(OLLAMA_TAGS, timeout=1.0)
        models = [m.get("name") for m in response.json().get("models", [])]
        return {"up": True, "models": models}
    except Exception:
        return {"up": False, "models": []}


def create_app(*, token: str | None = None, manager: JobManager | None = None,
               on_quit: Callable[[], None] | None = None,
               ollama: Callable[[], dict[str, Any]] = ollama_status) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    jobs = manager or JobManager()
    app = FastAPI(title="GreeceApt", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=2000)
    app.state.token = token
    app.state.jobs = jobs

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in _LOCAL_HOSTS and host != "testserver":
            return PlainTextResponse("local use only", status_code=403)
        if request.method != "GET" and request.headers.get("x-greeceapt-token") != token:
            return PlainTextResponse("missing or wrong token", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        version = str(int(max(f.stat().st_mtime for f in STATIC.iterdir())))  # no stale cached JS/CSS
        return page.replace("__TOKEN__", token).replace("__VERSION__", version)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ── data ─────────────────────────────────────────────────────────────────
    @app.get("/api/flats")
    def flats() -> JSONResponse:
        rows = queries.flats()
        thumbs = queries.thumbnails()
        for row in rows:
            row["photo"] = next((thumbs[k] for k in [row.get("opportunity_id"), row.get("url"), *(row.get("source_urls") or [])]
                                 if k and k in thumbs), None)
        return JSONResponse({"flats": rows, "neighborhoods": queries.neighborhood_summary(rows)})

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return queries.meta()

    @app.get("/api/flat/{opportunity_id}")
    def flat(opportunity_id: str) -> dict[str, Any]:
        detail = queries.flat_detail(opportunity_id)
        if detail is None:
            raise HTTPException(404, "no such flat in the current ranking")
        return detail

    @app.get("/api/history")
    def history() -> dict[str, Any]:
        out = queries.history()
        out["jobs"] = jobs.history.records[-100:][::-1]
        return out

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {"last_update": queries.last_update_text(), "ollama": ollama(),
                "ranking_built_at": queries.ranking_built_at(),
                "notices": queries.notices(), "busy": [j.to_json(tail=0) for j in jobs.running()],
                "outside_pipeline": jobs.external_pipelines()}

    @app.get("/api/review")
    def review_pairs() -> dict[str, Any]:
        held = review.held_urls(queries.flats())
        return {"pairs": review.pending(held_urls=held)}

    @app.post("/api/review")
    def review_decide(body: dict = Body(...)) -> dict[str, Any]:
        try:
            saved = review.decide(str(body.get("pair_key")), str(body.get("decision")))
        except KeyError:
            raise HTTPException(404, "no such pending pair") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"saved": saved}

    # ── actions and jobs ─────────────────────────────────────────────────────
    @app.get("/api/actions")
    def actions() -> list[dict[str, Any]]:
        return [{**a.to_json(), "params": resolve_params(a)} for a in ACTIONS.values()]

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [j.to_json() for j in jobs.list()]

    @app.post("/api/jobs")
    def start_job(body: dict = Body(...)) -> dict[str, Any]:
        key = str(body.get("action"))
        if key not in ACTIONS:
            raise HTTPException(404, "unknown action")
        try:
            params = validate_params(ACTIONS[key], body.get("params") or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if ACTIONS[key].network and body.get("confirmed") is not True:
            raise HTTPException(409, "network action needs confirmation")
        return jobs.submit(key, params).to_json()

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.stop(job_id).to_json()
        except KeyError:
            raise HTTPException(404, "no such job") from None

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    def job_log(job_id: str) -> str:
        try:
            return jobs.full_log(job_id)
        except KeyError:
            raise HTTPException(404, "no such job") from None

    @app.post("/api/quit")
    def quit_app() -> dict[str, Any]:
        running = [j.action.label for j in jobs.running()]
        if on_quit is not None:
            threading.Timer(0.3, on_quit).start()
        return {"quitting": True, "jobs_left_running": running}

    return app
