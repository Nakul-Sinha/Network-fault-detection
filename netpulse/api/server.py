"""Loopback control API and the local web UI.

PRD N7 and 12.2 set the security shape of this module and it is worth being
explicit about each part, because a local HTTP server on a user machine is
exactly the kind of thing that turns into a vulnerability when it is written
casually:

* **Loopback only.** The bind address is validated in config, so the API
  cannot be exposed on a routable interface by editing a setting.
* **Token on every API route.** A random token lives in an owner-only file
  under the state directory. The UI receives it by injection into the page
  the server itself serves, so it never travels through a query string and
  never lands in browser history.
* **Host header checked.** A loopback service that trusts the Host header is
  vulnerable to DNS rebinding: a page on the open internet can resolve its
  own hostname to 127.0.0.1 and then talk to this API from the browser. The
  middleware rejects any Host that is not a loopback literal.
* **No remote shell, no arbitrary paths.** Every route takes typed
  parameters; the only filesystem write is an export to a directory the
  agent already owns.
"""

from __future__ import annotations

import ipaddress
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import NetPulseConfig
from ..export.bundle import build_bundle
from ..features.schema import describe as describe_schema
from ..logging_setup import get_logger
from ..paths import api_token_file, data_dir, write_private
from ..store.repository import VALID_VERDICTS

log = get_logger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
TOKEN_HEADER = "X-NetPulse-Token"
TOKEN_PLACEHOLDER = "__NETPULSE_TOKEN__"


def load_or_create_token() -> str:
    """Read the API token, creating an owner-only file on first run."""
    path = api_token_file()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    write_private(path, token)
    log.info("created a new API token at %s", path)
    return token


def _is_loopback_host(host_header: str) -> bool:
    host = host_header.split(",")[0].strip()
    if not host:
        return False
    if host.startswith("["):  # bracketed IPv6
        host = host[1 : host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Request bodies live at module scope rather than inside create_app. With
# postponed annotation evaluation, FastAPI resolves a handler's type hints
# against the module namespace, so a model defined inside the factory is
# invisible to it and every POST body silently becomes a query parameter.


class LearningRequest(BaseModel):
    enabled: bool


class LabelRequest(BaseModel):
    verdict: str = Field(pattern="^(bad|ok|unsure)$")
    note: str = Field("", max_length=500)
    window_minutes: float = Field(15.0, ge=1.0, le=240.0)


class ExportRequest(BaseModel):
    hours: float = Field(24.0, ge=0.1, le=720.0)
    include_logs: bool = True


def create_app(agent: Any, config: NetPulseConfig | None = None) -> FastAPI:
    """Build the API around a running agent."""
    config = config or agent.config
    token = load_or_create_token()

    app = FastAPI(
        title="NetPulse Local",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.agent = agent
    app.state.token = token

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = request.headers.get("host", "")
        if not _is_loopback_host(host):
            # DNS rebinding defence: refuse anything that did not address us
            # as loopback, whatever the socket says.
            return PlainTextResponse("host not allowed", status_code=421)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        # The page loads only its own assets and talks only to itself.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        )
        return response

    def require_token(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, app.state.token):
            raise HTTPException(status_code=401, detail="a valid API token is required")

    auth = [Depends(require_token)]

    # ------------------------------------------------------------ read-only

    @app.get("/api/health", dependencies=auth)
    def health() -> dict[str, Any]:
        return agent.health_summary()

    @app.get("/api/status", dependencies=auth)
    def status() -> dict[str, Any]:
        return agent.status()

    @app.get("/api/timeline", dependencies=auth)
    def timeline(hours: float = Query(24.0, ge=0.1, le=720.0)) -> dict[str, Any]:
        since = time.time() - hours * 3600.0
        scores = agent.repo.scores_since(since, limit=5000)
        incidents = agent.repo.recent_incidents(limit=200, since=since)
        return {
            "since": since,
            "points": [
                {
                    "ts": score.ts,
                    "health": round(score.health, 1),
                    "risk_5m": round(score.risk_5m, 3),
                    "risk_15m": round(score.risk_15m, 3),
                    "severity": score.severity,
                    "primary_layer": score.primary_layer,
                }
                for score in scores
            ],
            "incidents": [incident.to_dict() for incident in incidents],
        }

    @app.get("/api/incidents", dependencies=auth)
    def incidents(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return [incident.to_dict() for incident in agent.repo.recent_incidents(limit=limit)]

    @app.get("/api/incidents/{incident_id}", dependencies=auth)
    def incident(incident_id: int) -> dict[str, Any]:
        found = agent.repo.get_incident(incident_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such incident")
        return found.to_dict()

    @app.get("/api/probes", dependencies=auth)
    def probes(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [record.to_dict() for record in agent.repo.recent_probes(limit=limit)]

    @app.get("/api/schema", dependencies=auth)
    def schema() -> list[dict[str, Any]]:
        return describe_schema()

    @app.get("/api/config", dependencies=auth)
    def get_config() -> dict[str, Any]:
        return config.redacted()

    @app.get("/api/drift", dependencies=auth)
    def drift(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return [event.to_dict() for event in agent.repo.recent_drift(limit=limit)]

    # --------------------------------------------------------------- control

    @app.post("/api/pause", dependencies=auth)
    def pause() -> dict[str, Any]:
        agent.pause()
        return {"paused": True}

    @app.post("/api/resume", dependencies=auth)
    def resume() -> dict[str, Any]:
        agent.resume()
        return {"paused": False}

    @app.post("/api/learning", dependencies=auth)
    def learning(request: LearningRequest) -> dict[str, Any]:
        agent.set_learning(request.enabled)
        return {"learning": request.enabled}

    @app.post("/api/label", dependencies=auth)
    def label(request: LabelRequest) -> dict[str, Any]:
        if request.verdict not in VALID_VERDICTS:
            raise HTTPException(status_code=422, detail="unknown verdict")
        created = agent.add_label(
            request.verdict, request.note, window_s=request.window_minutes * 60.0
        )
        return created.to_dict()

    @app.post("/api/export", dependencies=auth)
    def export(request: ExportRequest) -> dict[str, Any]:
        # The destination is always inside the agent data directory. There is
        # no caller-supplied path, so no route here can write anywhere else.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = data_dir() / "exports" / f"netpulse-bundle-{stamp}.zip"
        path = build_bundle(
            agent.repo,
            config,
            target,
            window_s=request.hours * 3600.0,
            include_logs=request.include_logs,
        )
        return {"path": str(path), "bytes": path.stat().st_size}

    @app.post("/api/wipe", dependencies=auth)
    def wipe() -> dict[str, Any]:
        agent.wipe()
        return {"wiped": True}

    # ------------------------------------------------------------------- UI

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        """Serve the UI with the API token injected.

        Injection rather than a query parameter: the token never appears in
        browser history, in a bookmark, or in a referrer, and a page from
        another origin cannot read this response to steal it.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, app.state.token))

    @app.get("/favicon.ico", response_model=None)
    def favicon() -> Response:
        icon = STATIC_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=404)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


def serve(agent: Any, config: NetPulseConfig | None = None) -> None:
    """Run the API in the foreground until interrupted."""
    import uvicorn

    config = config or agent.config
    app = create_app(agent, config)
    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_level="warning",
        access_log=False,
    )


def ui_url(config: NetPulseConfig) -> str:
    return f"http://{config.api.host}:{config.api.port}/"
