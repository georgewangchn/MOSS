from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
import asyncio
import os
from fastapi import FastAPI, HTTPException, Request, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from moss.agent_extractor import AgentExtractor
from moss.compiler import GovernanceCompiler
from moss.engine import GovernanceEngine
from moss.intent_agent import IntentAgent
from moss.models import DecisionStatus, Escalation, RiskLevel
from moss.owl_ontology import reason_graph
from moss.runtime import GovernanceRuntime
from moss.web.event_bus import event_bus, load_events

app = FastAPI(title="MOSS")

_STATIC_DIR = Path(__file__).parent / "static"

def _get_data_file() -> Path:
    """Resolve data file path consistently with Storage class."""
    from moss.config import get_config
    return get_config().data_path() / "governance_graph.json"

# ── CORS (configurable) ───────────────────────────────────────
_cors_env = os.getenv("MOSS_CORS_ORIGINS", "http://localhost:3000,http://localhost:8000")
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_methods=["*"], allow_headers=["*"])


# ── Bearer token auth ─────────────────────────────────────────
_API_TOKEN = os.getenv("MOSS_API_TOKEN", "")

# Paths that skip auth (health, static)
_OPEN_PATHS = {"/", "/api/status", "/favicon.ico"}


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token authentication for API endpoints.

    Skips auth for static pages and status endpoint.
    If MOSS_API_TOKEN is not set, auth is disabled (dev mode).
    """

    async def dispatch(self, request: Request, call_next):
        if not _API_TOKEN:
            # Dev mode: no token configured → no auth
            return await call_next(request)

        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {_API_TOKEN}":
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API token"},
        )

app.add_middleware(TokenAuthMiddleware)


class IngestRequest(BaseModel):
    text: str
    source: str = "web"


class AmendRequest(BaseModel):
    owner: Optional[str] = None
    deadline: Optional[str] = None
    status: Optional[DecisionStatus] = None
    department: Optional[str] = None
    title: Optional[str] = None
    progress: Optional[float] = None
    risk_level: Optional[RiskLevel] = None


def get_engine():
    return GovernanceEngine()


# ── Agent ────────────────────────────────────────────────

class ParseRequest(BaseModel):
    text: str


@app.post("/api/agent/parse")
async def agent_parse(req: ParseRequest):
    agent = IntentAgent()
    result = agent.parse(req.text)
    # If AMEND and has decision_title but no decision_id, look it up
    if result.get("intent") == "AMEND" and result.get("decision_title") and not result.get("decision_id"):
        engine = get_engine()
        for d in engine.list_decisions():
            if result["decision_title"][:6] in d.title:
                result["decision_id"] = d.id
                result["decision_title"] = d.title
                break
    return result


# ── Ingest ────────────────────────────────────────────────

@app.post("/api/ingest")
async def ingest(req: IngestRequest):
    engine = get_engine()
    agent = AgentExtractor()
    result = agent.ingest(req.text, source=req.source)
    if not result or not result.decisions:
        return {"decisions": [], "issues": [], "message": "No governance objects extracted"}

    outcome = engine.ingest_parsed_result(result)
    decisions = outcome["created"] + outcome["updated"]
    for d in decisions:
        await event_bus.emit("decision_created" if d in outcome["created"] else "decision_updated", {
            "id": d.id, "title": d.title, "owner": d.owner,
            "status": d.status, "source": req.source,
        })
    return {"decisions": [d.model_dump(mode="json") for d in outcome["created"]], "issues": result.issues}


# ── Decisions ─────────────────────────────────────────────

@app.get("/api/decisions")
def list_decisions(status: Optional[str] = None, department: Optional[str] = None):
    engine = get_engine()
    decisions = engine.list_decisions(status=status, department=department)
    return [d.model_dump(mode="json") for d in decisions]


@app.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str):
    engine = get_engine()
    d = engine.get_decision(decision_id)
    if not d:
        raise HTTPException(404, f"Decision {decision_id} not found")
    updates = engine.get_updates(decision_id)
    return {
        "decision": d.model_dump(mode="json"),
        "updates": [u.model_dump(mode="json") for u in updates],
    }


@app.patch("/api/decisions/{decision_id}")
async def amend_decision(decision_id: str, req: AmendRequest):
    engine = get_engine()
    decision = engine.get_decision(decision_id)
    if not decision:
        raise HTTPException(404)

    kwargs = {}
    if req.owner:
        engine.assign_owner(decision_id, req.owner, req.department or decision.department)
    if req.deadline:
        kwargs["deadline"] = datetime.fromisoformat(req.deadline)
    if req.status:
        engine.transition_status(decision_id, req.status)
    for k in ("title", "progress", "risk_level", "department"):
        v = getattr(req, k, None)
        if v is not None:
            kwargs[k] = v
    if kwargs:
        engine.update_decision(decision_id, **kwargs)

    await event_bus.emit("decision_updated", {
        "id": decision_id, "title": decision.title,
        "owner": decision.owner, "status": decision.status,
    })
    return engine.get_decision(decision_id).model_dump(mode="json")  # type: ignore[union-attr]


# ── Dashboard (aggregated: single HermiT call) ────────────

@app.get("/api/dashboard")
def dashboard():
    engine = get_engine()
    graph = engine.graph

    classified = reason_graph(graph)

    decisions = [d.model_dump(mode="json") for d in engine.list_decisions()]

    compiler = GovernanceCompiler()
    compile_results = [r.model_dump(mode="json") for r in compiler.compile(graph)]

    runtime = GovernanceRuntime(graph)
    alerts_list = [a.model_dump(mode="json") for a in runtime.checkpoint()]

    stats = engine.get_stats()

    return {
        "decisions": decisions,
        "compile": compile_results,
        "hermit": classified,
        "alerts": alerts_list,
        "stats": stats,
    }


# ── Governance ────────────────────────────────────────────

@app.get("/api/compile")
def compile():
    engine = get_engine()
    compiler = GovernanceCompiler()
    results = compiler.compile(engine.graph)
    return [r.model_dump(mode="json") for r in results]


@app.post("/api/checkpoint")
async def checkpoint():
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)
    alerts = runtime.checkpoint()
    escalations = runtime.auto_escalate(alerts)

    for a in alerts:
        await event_bus.emit("alert_triggered", {
            "decision_id": a.decision_id,
            "decision_title": a.decision_title,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "message": a.message,
        }, severity=a.severity.lower())

    return {
        "alerts": [a.model_dump(mode="json") for a in alerts],
        "escalations": [e.model_dump(mode="json") for e in escalations],
    }


@app.get("/api/alerts")
def alerts():
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)
    alerts = runtime.checkpoint()
    return [a.model_dump(mode="json") for a in alerts]


@app.get("/api/status")
def status():
    engine = get_engine()
    return engine.get_stats()


@app.get("/api/brief")
def brief():
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)
    return {"brief": runtime.generate_meeting_brief()}


@app.get("/api/graph")
def graph():
    engine = get_engine()
    g = engine.graph
    return {
        "decisions": {k: v.model_dump(mode="json") for k, v in g.decisions.items()},
        "edges": g.edges,
    }


@app.get("/api/hermit")
def hermit():
    engine = get_engine()
    return reason_graph(engine.graph)


# ── Events ──────────────────────────────────────────────

@app.get("/api/events")
def get_events(limit: int = 100):
    return load_events(limit)


# ── Events (SSE) ─────────────────────────────────────────

@app.get("/api/events/stream")
async def event_stream():
    async def generate():
        async for event in event_bus.subscribe():
            yield event.to_sse()
    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Static ────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(_STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── File Watcher (CLI ↔ Web sync) ────────────────────────

_watch_task = None
_last_mtime: float = 0
_last_emit: float = 0


async def _watch_data_file():
    global _last_mtime, _last_emit
    import time as _time
    data_file = _get_data_file()
    while True:
        await asyncio.sleep(3)
        try:
            if data_file.exists():
                mtime = data_file.stat().st_mtime
                now = _time.time()
                if mtime != _last_mtime and (now - _last_emit) > 5:
                    _last_mtime = mtime
                    _last_emit = now
                    await event_bus.emit("data_changed", {"source": "file_watcher"})
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    global _watch_task
    _watch_task = asyncio.create_task(_watch_data_file())


@app.on_event("shutdown")
async def shutdown():
    global _watch_task
    if _watch_task:
        _watch_task.cancel()


# ── Startup ──────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import uvicorn
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
