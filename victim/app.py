"""
PagedOut victim app — a small microservice that can be broken on purpose.

Three instances of this image form a dependency chain:

    checkout-service ──► payment-service ──► ledger-service

Each instance is configured entirely by environment variables, so the same
image runs as any of the three. Every instance exposes:

  business    GET  /api/process          do work, call downstream
  health      GET  /health               liveness + current fault state
  metrics     GET  /metrics              Prometheus exposition
  chaos       POST /chaos/{fault}        inject a fault
              POST /chaos/clear          remove all faults
  admin       POST /admin/pool/resize    real remediation target
              POST /admin/pool/drain     real remediation target
              POST /admin/cache/clear    real remediation target
              POST /admin/version/rollback  real remediation target

The admin endpoints exist so the remediation agent has something real to act
on. An action either measurably changes this service's state or it does not —
that is what makes the Phase 5 numbers mean anything.
"""

import asyncio
import json
import logging
import os
import random
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

# ── Configuration ─────────────────────────────────────────────────────────────

SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown-service")
DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "")
PORT = int(os.getenv("PORT", "8000"))
POOL_SIZE_DEFAULT = int(os.getenv("POOL_SIZE", "20"))
VERSION_DEFAULT = os.getenv("APP_VERSION", "v2.3.0")


# ── Structured logging to stdout ──────────────────────────────────────────────


# A log line's severity and the incident it belongs to are things only this
# service knows. Emitting them here means the log shipper stays dumb and the
# records land in Kafka already conforming to the incident-signal schema that
# the Flink job consumes — the same schema the synthetic generator produces.
LEVEL_TO_SEVERITY = {
    "FATAL": "P1",
    "CRITICAL": "P1",
    "ERROR": "P1",
    "WARNING": "P2",
    "WARN": "P2",
    "INFO": "P3",
    "DEBUG": "P3",
}

FAULT_TO_INCIDENT = {
    "pool_exhaustion": "database_connection_exhaustion",
    "memory_leak": "memory_leak",
    "latency_spike": "high_latency_spike",
    "error_burst": "pod_crash_loop",
    "dependency_timeout": "network_partition",
    "bad_deploy": "deployment_failure",
}

# In Docker the hostname is the short container id, which is the closest
# analogue this stack has to a pod name.
POD_NAME = f"{SERVICE_NAME}-{socket.gethostname()}"


def _current_incident_type() -> str:
    """Label logs with the fault currently active, if any.

    Sorted so the label is deterministic when several faults overlap;
    otherwise set iteration order would make the stream non-reproducible
    across runs, which would undermine evaluation.
    """
    active = sorted(state.faults) if "state" in globals() else []
    for fault in active:
        mapped = FAULT_TO_INCIDENT.get(fault)
        if mapped:
            return mapped
    return "healthy"


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per line so a log shipper can parse without regex."""

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        payload = {
            "event_id": str(uuid.uuid4()),
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "service": SERVICE_NAME,
            "incident_type": _current_incident_type(),
            "severity": LEVEL_TO_SEVERITY.get(level, "P3"),
            "level": level,
            "message": record.getMessage(),
            "pod": POD_NAME,
            "namespace": "production",
            "emitted_at_ms": int(record.created * 1000),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        return json.dumps(payload)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonLogFormatter())
log = logging.getLogger(SERVICE_NAME)
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False


def emit(level: str, message: str, **fields: Any) -> None:
    record_level = getattr(logging, level.upper(), logging.INFO)
    log.log(record_level, message, extra={"extra_fields": fields})


# ── Prometheus metrics ────────────────────────────────────────────────────────

REQUESTS = Counter(
    "pagedout_requests_total",
    "Total requests handled",
    ["service", "endpoint", "outcome"],
)
LATENCY = Histogram(
    "pagedout_request_duration_seconds",
    "Request duration",
    ["service", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
POOL_IN_USE = Gauge(
    "pagedout_db_connections_in_use", "Connections currently checked out", ["service"]
)
POOL_SIZE = Gauge(
    "pagedout_db_connections_max", "Connection pool capacity", ["service"]
)
HEAP_PCT = Gauge("pagedout_heap_usage_pct", "Simulated heap usage", ["service"])
CACHE_ENTRIES = Gauge("pagedout_cache_entries", "Entries in local cache", ["service"])
ACTIVE_FAULTS = Gauge(
    "pagedout_active_faults", "Number of injected faults", ["service"]
)
BUILD_INFO = Gauge(
    "pagedout_build_info", "Deployed version, value is always 1", ["service", "version"]
)


# ── Mutable service state ─────────────────────────────────────────────────────


class ServiceState:
    """Everything a remediation action is allowed to change."""

    def __init__(self) -> None:
        self.pool_size = POOL_SIZE_DEFAULT
        self.pool_in_use = 0
        self.version = VERSION_DEFAULT
        self.previous_version = "v2.2.8"
        self.cache: dict[str, float] = {}
        self.ballast: list[bytearray] = []
        self.faults: set[str] = set()
        self.started_at = time.time()
        self.lock = asyncio.Lock()

    @property
    def heap_pct(self) -> float:
        # 12% baseline plus whatever the ballast is holding, capped at 99.
        return min(99.0, 12.0 + len(self.ballast) * 3.0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": SERVICE_NAME,
            "version": self.version,
            "pool_size": self.pool_size,
            "pool_in_use": self.pool_in_use,
            "pool_utilization": round(self.pool_in_use / max(self.pool_size, 1), 3),
            "heap_pct": round(self.heap_pct, 1),
            "cache_entries": len(self.cache),
            "active_faults": sorted(self.faults),
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }


state = ServiceState()


def refresh_metrics() -> None:
    POOL_IN_USE.labels(SERVICE_NAME).set(state.pool_in_use)
    POOL_SIZE.labels(SERVICE_NAME).set(state.pool_size)
    HEAP_PCT.labels(SERVICE_NAME).set(state.heap_pct)
    CACHE_ENTRIES.labels(SERVICE_NAME).set(len(state.cache))
    ACTIVE_FAULTS.labels(SERVICE_NAME).set(len(state.faults))
    BUILD_INFO.labels(SERVICE_NAME, state.version).set(1)


# ── Fault behaviour ───────────────────────────────────────────────────────────

VALID_FAULTS = {
    "pool_exhaustion",  # connection pool saturates, requests queue then fail
    "memory_leak",      # heap climbs until it trips the OOM threshold
    "latency_spike",    # every request sleeps
    "error_burst",      # a fraction of requests return 500
    "dependency_timeout",  # downstream calls hang
    "bad_deploy",       # version bumps and error rate jumps with it
}


async def apply_request_faults(endpoint: str) -> None:
    """Raise or stall according to whichever faults are currently active."""

    if "latency_spike" in state.faults:
        await asyncio.sleep(random.uniform(1.2, 3.0))

    if "memory_leak" in state.faults:
        # 4 MB per request, which walks the heap gauge upward over time.
        state.ballast.append(bytearray(4 * 1024 * 1024))
        if state.heap_pct > 92:
            emit(
                "ERROR",
                "GC overhead limit exceeded",
                heap_pct=round(state.heap_pct, 1),
                gc_pause_ms=random.randint(1800, 3200),
            )
            raise HTTPException(status_code=503, detail="heap exhausted")

    if "error_burst" in state.faults and random.random() < 0.45:
        emit("ERROR", "Unhandled exception in request handler", endpoint=endpoint)
        raise HTTPException(status_code=500, detail="internal error")

    if "bad_deploy" in state.faults and random.random() < 0.6:
        emit(
            "ERROR",
            "NullPointerException in OrderValidator",
            endpoint=endpoint,
            version=state.version,
        )
        raise HTTPException(status_code=500, detail="regression in current build")


@asynccontextmanager
async def checkout_connection():
    """Borrow a pooled connection, or fail the way a real pool fails."""

    async with state.lock:
        saturated = state.pool_in_use >= state.pool_size
        if "pool_exhaustion" in state.faults and saturated:
            emit(
                "FATAL",
                f"Connection pool exhausted. Active connections: "
                f"{state.pool_in_use}/{state.pool_size}",
                pool_in_use=state.pool_in_use,
                pool_size=state.pool_size,
            )
            raise HTTPException(status_code=503, detail="connection pool exhausted")
        state.pool_in_use += 1

    try:
        yield
    finally:
        async with state.lock:
            state.pool_in_use = max(0, state.pool_in_use - 1)


# ── Application ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    refresh_metrics()
    emit("INFO", f"{SERVICE_NAME} started", version=state.version, port=PORT)
    leak_task = asyncio.create_task(_background_pressure())
    yield
    leak_task.cancel()


async def _background_pressure() -> None:
    """Hold connections open while pool_exhaustion is active.

    Without this the pool only saturates under concurrent load. Holding a
    slice of the pool makes the fault reproducible from a single request,
    which matters for deterministic evaluation runs.
    """
    while True:
        await asyncio.sleep(1.0)
        async with state.lock:
            if "pool_exhaustion" in state.faults:
                # Saturate fully. Leaving a free slot would mean the fault
                # injects but never actually fails a request.
                state.pool_in_use = min(state.pool_size, state.pool_in_use + 5)
            elif state.pool_in_use > 0 and not state.faults:
                state.pool_in_use = max(0, state.pool_in_use - 2)
        refresh_metrics()


app = FastAPI(title=f"PagedOut victim — {SERVICE_NAME}", lifespan=lifespan)


@app.get("/api/process")
async def process() -> dict[str, Any]:
    start = time.perf_counter()
    endpoint = "/api/process"
    try:
        await apply_request_faults(endpoint)

        async with checkout_connection():
            state.cache[f"key-{random.randint(0, 5000)}"] = time.time()
            downstream: dict[str, Any] | None = None

            if DOWNSTREAM_URL:
                timeout = 30.0 if "dependency_timeout" in state.faults else 2.0
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.get(f"{DOWNSTREAM_URL}/api/process")
                        # httpx does not raise on 5xx, only on transport
                        # errors. Without this the chain would report success
                        # while its dependency is down, and cascade_failure
                        # would never be observable upstream.
                        resp.raise_for_status()
                        downstream = resp.json()
                except httpx.HTTPError as exc:
                    emit(
                        "ERROR",
                        f"Downstream call failed: {type(exc).__name__}",
                        downstream=DOWNSTREAM_URL,
                    )
                    REQUESTS.labels(SERVICE_NAME, endpoint, "downstream_error").inc()
                    raise HTTPException(status_code=502, detail="downstream failure")

        REQUESTS.labels(SERVICE_NAME, endpoint, "success").inc()
        return {
            "service": SERVICE_NAME,
            "version": state.version,
            "downstream": downstream,
        }

    except HTTPException:
        REQUESTS.labels(SERVICE_NAME, endpoint, "error").inc()
        raise
    finally:
        LATENCY.labels(SERVICE_NAME, endpoint).observe(time.perf_counter() - start)
        refresh_metrics()


@app.get("/health")
async def health() -> dict[str, Any]:
    snap = state.snapshot()
    snap["healthy"] = not state.faults
    return snap


@app.get("/metrics")
async def metrics() -> Response:
    refresh_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Chaos control ─────────────────────────────────────────────────────────────


@app.post("/chaos/clear")
async def chaos_clear() -> dict[str, Any]:
    state.faults.clear()
    state.ballast.clear()
    state.pool_in_use = 0
    refresh_metrics()
    emit("INFO", "All injected faults cleared")
    return state.snapshot()


@app.post("/chaos/{fault}")
async def chaos_inject(fault: str) -> dict[str, Any]:
    if fault not in VALID_FAULTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown fault '{fault}'; valid: {sorted(VALID_FAULTS)}",
        )
    state.faults.add(fault)

    if fault == "bad_deploy":
        state.previous_version = state.version
        state.version = "v2.3.1"
        emit("WARN", f"Deployed {state.version}", previous=state.previous_version)

    refresh_metrics()
    emit("WARN", f"Fault injected: {fault}", fault=fault)
    return state.snapshot()


# ── Admin endpoints — the real remediation surface ────────────────────────────


class ResizeRequest(BaseModel):
    size: int


@app.post("/admin/pool/resize")
async def pool_resize(req: ResizeRequest) -> dict[str, Any]:
    if not 1 <= req.size <= 500:
        raise HTTPException(status_code=400, detail="size must be within 1..500")
    before = state.pool_size
    state.pool_size = req.size
    refresh_metrics()
    emit("INFO", f"Connection pool resized {before} -> {req.size}")
    return {"changed": before != req.size, "before": before, "after": req.size}


@app.post("/admin/pool/drain")
async def pool_drain() -> dict[str, Any]:
    before = state.pool_in_use
    state.pool_in_use = 0
    state.faults.discard("pool_exhaustion")
    refresh_metrics()
    emit("INFO", f"Drained {before} idle connections")
    return {"changed": before > 0, "drained": before}


@app.post("/admin/cache/clear")
async def cache_clear() -> dict[str, Any]:
    before = len(state.cache)
    state.cache.clear()
    state.ballast.clear()
    state.faults.discard("memory_leak")
    refresh_metrics()
    emit("INFO", f"Cleared {before} cache entries and released heap ballast")
    return {"changed": before > 0, "cleared": before}


@app.post("/admin/version/rollback")
async def version_rollback() -> dict[str, Any]:
    if state.version == state.previous_version:
        return {"changed": False, "version": state.version}
    rolled_from = state.version
    state.version = state.previous_version
    state.faults.discard("bad_deploy")
    refresh_metrics()
    emit("INFO", f"Rolled back {rolled_from} -> {state.version}")
    return {"changed": True, "from": rolled_from, "to": state.version}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
