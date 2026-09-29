"""
PagedOut API — read model for the dashboard, plus control endpoints.

Deliberately thin. It composes the pieces that already exist rather than
reimplementing them:

    victim services   live health and fault state
    Prometheus        time series
    Qdrant            retrieval
    Groundhog         simulation results from the last sweep

Everything is read-only except /chaos and /remediate, which exist so the
dashboard can drive a live demo: break something, watch it get detected,
watch it get fixed.

Run:
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "remediation"))
sys.path.insert(0, str(ROOT / "groundhog"))

BENCHMARKS = ROOT / "docs" / "benchmarks"
FRONTEND = ROOT / "frontend"

app = FastAPI(
    title="PagedOut API",
    description="Autonomous incident remediation, validated by deterministic "
                "simulation testing.",
    version="1.0.0",
)

# The dashboard is served from this same origin in production, but during
# development it runs on a different port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173",
                   "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────


class ChaosRequest(BaseModel):
    service: str = Field(description="checkout-service | payment-service | ledger-service")
    fault: str = Field(description="pool_exhaustion | memory_leak | latency_spike | "
                                   "error_burst | dependency_timeout | bad_deploy")


class RemediateRequest(BaseModel):
    service: str
    incident_type: str = "database_connection_exhaustion"
    approve_high_risk: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_benchmark(name: str) -> dict[str, Any]:
    path = BENCHMARKS / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}


# ── System ────────────────────────────────────────────────────────────────────


@app.get("/api/health", tags=["system"])
def health() -> dict[str, Any]:
    """Liveness for the API itself, plus reachability of each dependency.

    Reports per-dependency status rather than one boolean, so a partially
    degraded stack is visible instead of just "unhealthy".
    """
    from tools import PROMETHEUS, SERVICE_PORTS, check_health

    deps: dict[str, str] = {}
    for service in SERVICE_PORTS:
        ev = check_health(service)
        deps[service] = "up" if ev.values.get("reachable") else "down"

    import urllib.request
    for name, url in (("prometheus", f"{PROMETHEUS}/-/healthy"),
                      ("qdrant", "http://localhost:6333/readyz")):
        try:
            with urllib.request.urlopen(url, timeout=2):
                deps[name] = "up"
        except Exception:
            deps[name] = "down"

    return {"status": "ok", "dependencies": deps, "checked_at": time.time()}


@app.get("/api/services", tags=["incidents"])
def services() -> list[dict[str, Any]]:
    """Live state of every victim service, straight from /health."""
    from tools import DEPENDENCIES, SERVICE_PORTS, check_health

    out = []
    for name in SERVICE_PORTS:
        ev = check_health(name)
        out.append({
            "service": name,
            "reachable": ev.values.get("reachable", False),
            "healthy": ev.values.get("healthy", False),
            "version": ev.values.get("version"),
            "pool_utilization": ev.values.get("pool_utilisation"),
            "heap_pct": ev.values.get("heap_pct"),
            "active_faults": ev.values.get("active_faults", []),
            "depends_on": DEPENDENCIES.get(name, []),
        })
    return out


@app.get("/api/evidence/{service}", tags=["incidents"])
def evidence(service: str, incident_type: str = "database_connection_exhaustion"):
    """Run the investigator's probes and return the raw evidence.

    This is the endpoint that demonstrates cascade attribution: ask about a
    service that is merely a victim and the dependency probe says so.
    """
    from tools import SERVICE_PORTS, gather_evidence

    if service not in SERVICE_PORTS:
        raise HTTPException(404, f"unknown service {service!r}")

    findings = gather_evidence(service, incident_type)
    cascade_origin = next(
        (f.values["unhealthy"][0] for f in findings if f.values.get("unhealthy")),
        None,
    )
    return {
        "service": service,
        "incident_type": incident_type,
        "cascade_origin": cascade_origin,
        "is_cascade_victim": cascade_origin is not None,
        "evidence": [
            {"tool": f.tool, "summary": f.summary, "values": f.values, "ok": f.ok}
            for f in findings
        ],
    }


# ── Chaos + remediation ───────────────────────────────────────────────────────


@app.post("/api/chaos", tags=["demo"])
def inject(req: ChaosRequest) -> dict[str, Any]:
    from ports import HttpTransport
    from tools import SERVICE_PORTS

    if req.service not in SERVICE_PORTS:
        raise HTTPException(404, f"unknown service {req.service!r}")

    resp = HttpTransport(SERVICE_PORTS).post(req.service, f"/chaos/{req.fault}")
    if not resp.ok:
        raise HTTPException(400, resp.error or f"status {resp.status}")
    return {"injected": req.fault, "service": req.service, "state": resp.body}


@app.post("/api/chaos/clear", tags=["demo"])
def clear() -> dict[str, Any]:
    from ports import HttpTransport
    from tools import SERVICE_PORTS

    transport = HttpTransport(SERVICE_PORTS)
    return {
        "cleared": [s for s in SERVICE_PORTS
                    if transport.post(s, "/chaos/clear").ok]
    }


@app.post("/api/remediate", tags=["demo"])
def remediate(req: RemediateRequest) -> dict[str, Any]:
    """Plan and execute remediation through the real safe-execution path.

    Uses the registry's low-risk remedy for the incident type rather than
    invoking the LLM, so the dashboard stays responsive — an LLM plan takes
    ~30s on a local 3.8B model. The execution path is the real one:
    idempotency keys, dry-run, verify, rollback.
    """
    from executor import RemediationExecutor
    from ports import Ports
    from registry import Risk, actions_for
    from tools import SERVICE_PORTS

    if req.service not in SERVICE_PORTS:
        raise HTTPException(404, f"unknown service {req.service!r}")

    candidates = actions_for(req.incident_type, Risk.LOW)
    if not candidates:
        return {"outcome": "no_low_risk_action", "incident_type": req.incident_type}

    spec = candidates[0]
    incident_id = f"api-{req.service}-{int(time.time())}"
    ports = Ports.production(SERVICE_PORTS)
    executor = RemediationExecutor(ports)

    result = executor.execute_action(incident_id, spec.name.value, req.service, {})
    return {
        "incident_id": incident_id,
        "action": result.action,
        "target": result.target,
        "outcome": result.outcome.value,
        "changed": result.changed,
        "attempts": result.attempts,
        "detail": result.detail,
        "idempotency_key": result.key,
        "health_before": result.health_before,
        "health_after": result.health_after,
    }


# ── Retrieval ─────────────────────────────────────────────────────────────────


@app.get("/api/retrieve", tags=["rag"])
def retrieve(q: str, mode: str = "hybrid", top_k: int = 3):
    """Query the runbook corpus. `mode` is dense | sparse | hybrid.

    Exposed with a mode switch on purpose: it lets the dashboard show the
    same query returning a wrong answer under dense-only and the right one
    under hybrid, which is the clearest demonstration of why hybrid matters.
    """
    from retrieve import HybridRetriever

    if mode not in ("dense", "sparse", "hybrid"):
        raise HTTPException(400, "mode must be dense, sparse or hybrid")

    global _retriever
    try:
        _retriever
    except NameError:
        _retriever = HybridRetriever()

    hits, ms = _retriever.search_timed(q, top_k=top_k, mode=mode)
    return {
        "query": q,
        "mode": mode,
        "latency_ms": round(ms, 2),
        "hits": [
            {"doc_id": h.doc_id, "title": h.title, "source": h.source,
             "score": round(h.score, 4), "incident_type": h.incident_type,
             "steps": h.steps[:6]}
            for h in hits
        ],
    }


# ── Benchmarks ────────────────────────────────────────────────────────────────


@app.get("/api/benchmarks", tags=["results"])
def benchmarks() -> dict[str, Any]:
    """Every measured result, for the dashboard's numbers."""
    return {
        "ingestion": _load_benchmark("loadtest_results.json"),
        "retrieval": _load_benchmark("retrieval_results.json"),
        "groundhog": _load_benchmark("groundhog_results.json"),
    }


@app.get("/api/groundhog/trace/{seed}", tags=["results"])
def trace(seed: int, scenario: str = "pool_exhaustion_drain"):
    """Re-run a seed and return its trace.

    The whole point of determinism: a trace is not stored, it is
    REGENERATED on demand and is guaranteed identical to the original run.
    """
    from runner import SCENARIOS, run

    match = next((s for s in SCENARIOS if s.name == scenario), None)
    if match is None:
        raise HTTPException(404, f"unknown scenario {scenario!r}; "
                                 f"have {[s.name for s in SCENARIOS]}")

    result = run(seed, match)
    return {
        "seed": seed,
        "scenario": scenario,
        "digest": result.trace.digest(),
        "crashed": result.crashed,
        "recovered": result.recovered,
        "side_effects": result.side_effect_count,
        "violations": [
            {"invariant": v.invariant, "detail": v.detail, "evidence": v.evidence}
            for v in result.violations
        ],
        "events": [
            {"at_s": round(e.at / 1_000_000, 4), "kind": e.kind.value,
             "actor": e.actor, "detail": e.detail, "data": e.data}
            for e in result.trace.events
        ],
    }


@app.get("/api/groundhog/scenarios", tags=["results"])
def scenarios() -> list[dict[str, Any]]:
    from runner import SCENARIOS

    return [
        {"name": s.name, "incident_type": s.incident_type, "target": s.target,
         "actions": [a["action"] for a in s.plan["actions"]],
         "approve_high_risk": s.approve_high_risk}
        for s in SCENARIOS
    ]


# ── Static frontend ───────────────────────────────────────────────────────────

if FRONTEND.exists() and (FRONTEND / "index.html").exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(str(FRONTEND / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
