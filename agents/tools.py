"""
Real investigation tools.

Replaces the previous implementation, where `query_prometheus` returned
`random.uniform(0.3, 0.9)` and `check_recent_deployments` returned a
hardcoded string claiming a deploy happened four minutes ago. An agent
reasoning over fabricated evidence is not an agent.

Every function here queries something that actually exists:
  - Prometheus, scraping the live victim services every 5s
  - the services' own /health endpoints
  - the declared dependency topology

Each returns an `Evidence` record rather than a formatted string, so the
planner can branch on numbers while the LLM gets prose. Returning only
prose would force the planner to re-parse text the tool already had
structured.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

PROMETHEUS = "http://localhost:9090"

# Host ports for the victim services, matching docker-compose.
SERVICE_PORTS = {
    "checkout-service": 8081,
    "payment-service": 8082,
    "ledger-service": 8083,
}

# The declared call graph. Real topology, not a guess: it matches the
# DOWNSTREAM_URL wiring in docker-compose.
DEPENDENCIES = {
    "checkout-service": ["payment-service"],
    "payment-service": ["ledger-service"],
    "ledger-service": [],
}


@dataclass
class Evidence:
    """One investigative finding."""

    tool: str
    summary: str                      # human/LLM readable
    values: dict[str, Any] = field(default_factory=dict)  # machine readable
    ok: bool = True                   # False when the tool itself failed

    def __str__(self) -> str:
        return f"[{self.tool}] {self.summary}"


# ── Prometheus ────────────────────────────────────────────────────────────────


def _promql(query: str, timeout: float = 5.0) -> list[dict]:
    """Run an instant PromQL query, returning the raw result vector.

    Returns [] on any failure rather than raising. An investigation that
    dies because one metric is unavailable is worse than one that reports
    partial evidence — and a real on-call engineer degrades the same way.
    """
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    if payload.get("status") != "success":
        return []
    return payload.get("data", {}).get("result", [])


def _scalar(query: str) -> float | None:
    rows = _promql(query)
    if not rows:
        return None
    try:
        return float(rows[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def query_pool_utilisation(service: str) -> Evidence:
    """Connection pool usage, the signal for database_connection_exhaustion."""
    in_use = _scalar(f'pagedout_db_connections_in_use{{service="{service}"}}')
    capacity = _scalar(f'pagedout_db_connections_max{{service="{service}"}}')

    if in_use is None or capacity is None:
        return Evidence("prometheus.pool", f"no pool metrics for {service}", ok=False)

    util = in_use / capacity if capacity else 0.0
    verdict = "SATURATED" if util >= 1.0 else "elevated" if util >= 0.8 else "normal"
    return Evidence(
        "prometheus.pool",
        f"{service} connection pool {in_use:.0f}/{capacity:.0f} "
        f"({util:.0%}) — {verdict}",
        {"in_use": in_use, "capacity": capacity, "utilisation": round(util, 3),
         "saturated": util >= 1.0},
    )


def query_error_rate(service: str, window: str = "1m") -> Evidence:
    """Fraction of requests failing, computed from the Counter."""
    q = (
        f'sum(rate(pagedout_requests_total{{service="{service}",outcome!="success"}}[{window}]))'
        f' / clamp_min(sum(rate(pagedout_requests_total{{service="{service}"}}[{window}])), 0.001)'
    )
    rate = _scalar(q)
    if rate is None:
        return Evidence("prometheus.errors", f"no request metrics for {service}", ok=False)

    verdict = "critical" if rate >= 0.5 else "elevated" if rate >= 0.05 else "normal"
    return Evidence(
        "prometheus.errors",
        f"{service} error rate {rate:.1%} over {window} — {verdict}",
        {"error_rate": round(rate, 4), "window": window},
    )


def query_latency_p99(service: str, window: str = "1m") -> Evidence:
    """p99 request latency, derived from the Histogram buckets."""
    q = (
        f'histogram_quantile(0.99, sum(rate('
        f'pagedout_request_duration_seconds_bucket{{service="{service}"}}[{window}]'
        f')) by (le))'
    )
    seconds = _scalar(q)
    if seconds is None:
        return Evidence("prometheus.latency", f"no latency histogram for {service}", ok=False)

    ms = seconds * 1000
    verdict = "critical" if ms >= 1000 else "elevated" if ms >= 500 else "normal"
    return Evidence(
        "prometheus.latency",
        f"{service} p99 latency {ms:.0f}ms over {window} — {verdict}",
        {"p99_ms": round(ms, 1), "window": window},
    )


def query_heap(service: str) -> Evidence:
    """Heap usage percent, the signal for memory_leak."""
    pct = _scalar(f'pagedout_heap_usage_pct{{service="{service}"}}')
    if pct is None:
        return Evidence("prometheus.heap", f"no heap metrics for {service}", ok=False)

    verdict = "critical" if pct >= 90 else "elevated" if pct >= 75 else "normal"
    return Evidence(
        "prometheus.heap",
        f"{service} heap at {pct:.0f}% — {verdict}",
        {"heap_pct": round(pct, 1), "critical": pct >= 90},
    )


def query_deployed_version(service: str) -> Evidence:
    """Currently deployed version, from the build_info gauge.

    Replaces a hardcoded string that always claimed a deploy happened four
    minutes ago — which made every incident look like a bad deploy.
    """
    rows = _promql(f'pagedout_build_info{{service="{service}"}}')
    if not rows:
        return Evidence("prometheus.version", f"no build info for {service}", ok=False)

    versions = [r["metric"].get("version", "unknown") for r in rows]
    return Evidence(
        "prometheus.version",
        f"{service} running version(s): {', '.join(sorted(set(versions)))}",
        {"versions": sorted(set(versions))},
    )


# ── Direct service introspection ──────────────────────────────────────────────


def check_health(service: str, timeout: float = 3.0) -> Evidence:
    """Call the service's own /health endpoint.

    Prometheus is a 5-second-resolution sampled view. /health is ground
    truth right now, and it reports which faults are actually active —
    which is how we can tell a real root cause from a correlated symptom.
    """
    port = SERVICE_PORTS.get(service)
    if not port:
        return Evidence("health", f"{service} is not a local victim service", ok=False)

    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health", timeout=timeout
        ) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return Evidence(
            "health",
            f"{service} health check failed: {type(exc).__name__}",
            {"reachable": False},
            ok=False,
        )

    faults = data.get("active_faults", [])
    return Evidence(
        "health",
        f"{service} healthy={data.get('healthy')} "
        f"version={data.get('version')} "
        f"pool={data.get('pool_in_use')}/{data.get('pool_size')} "
        f"heap={data.get('heap_pct')}% "
        f"faults={faults or 'none'}",
        {
            "reachable": True,
            "healthy": data.get("healthy", False),
            "version": data.get("version"),
            "pool_utilisation": data.get("pool_utilization"),
            "heap_pct": data.get("heap_pct"),
            "active_faults": faults,
        },
    )


def check_dependencies(service: str) -> Evidence:
    """Walk the dependency chain and report which downstream services are sick.

    This is what distinguishes a root cause from a cascade. If checkout is
    failing but payment is also failing, checkout is a victim, not the cause.
    """
    deps = DEPENDENCIES.get(service, [])
    if not deps:
        return Evidence(
            "dependencies",
            f"{service} has no downstream dependencies — it is a leaf service",
            {"dependencies": [], "unhealthy": []},
        )

    unhealthy: list[str] = []
    details: dict[str, Any] = {}
    for dep in deps:
        ev = check_health(dep)
        details[dep] = ev.values
        if not ev.values.get("healthy", False):
            unhealthy.append(dep)

    if unhealthy:
        summary = (
            f"{service} depends on {', '.join(deps)}; "
            f"UNHEALTHY: {', '.join(unhealthy)} — "
            f"{service} is likely a cascade victim, not the origin"
        )
    else:
        summary = f"{service} depends on {', '.join(deps)}; all healthy"

    return Evidence(
        "dependencies",
        summary,
        {"dependencies": deps, "unhealthy": unhealthy, "detail": details},
    )


# ── Orchestration ─────────────────────────────────────────────────────────────

# Which probes are worth running for which incident type. Running all of them
# every time would be slower and would bury the relevant signal in noise.
PROBES_BY_TYPE = {
    "database_connection_exhaustion": ("pool", "errors", "health", "deps"),
    "memory_leak": ("heap", "errors", "health", "deps"),
    "high_latency_spike": ("latency", "errors", "health", "deps"),
    "pod_crash_loop": ("errors", "health", "deps"),
    "deployment_failure": ("version", "errors", "health", "deps"),
    "network_partition": ("deps", "errors", "health"),
    "cascade_failure": ("deps", "errors", "health"),
    "cpu_throttling": ("latency", "errors", "health"),
    "disk_space_critical": ("errors", "health"),
}
DEFAULT_PROBES = ("health", "errors", "deps")

_PROBE_FUNCS = {
    "pool": query_pool_utilisation,
    "errors": query_error_rate,
    "latency": query_latency_p99,
    "heap": query_heap,
    "version": query_deployed_version,
    "health": check_health,
    "deps": check_dependencies,
}


def gather_evidence(service: str, incident_type: str) -> list[Evidence]:
    """Run the probes relevant to this incident type, in order."""
    names = PROBES_BY_TYPE.get(incident_type, DEFAULT_PROBES)
    out: list[Evidence] = []
    for name in names:
        func = _PROBE_FUNCS.get(name)
        if func:
            out.append(func(service))
    return out


if __name__ == "__main__":
    import sys

    svc = sys.argv[1] if len(sys.argv) > 1 else "payment-service"
    itype = sys.argv[2] if len(sys.argv) > 2 else "database_connection_exhaustion"
    print(f"Evidence for {svc} / {itype}:\n")
    for ev in gather_evidence(svc, itype):
        mark = " " if ev.ok else "!"
        print(f" {mark} {ev}")
