"""
PagedOut - Investigator Agent
Investigates the incident by calling tools directly.
Uses phi3:mini only for final root cause summary.
No tool calling via LLM - avoids phi3:mini limitation.
"""

import json
import random
from datetime import datetime
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage


# ── TOOL FUNCTIONS (called directly, not via LLM) ─────────────────────────────

def query_prometheus(service: str, metric: str) -> str:
    """Query Prometheus for current metric value."""
    metrics = {
        "error_rate": round(random.uniform(0.3, 0.9), 2),
        "db_connections": random.randint(85, 100),
        "latency_p99": random.randint(2000, 8000),
        "memory_usage": random.randint(80, 99),
        "cpu_usage": random.randint(70, 95),
    }
    value = metrics.get(metric, round(random.uniform(0, 100), 2))
    return f"{service} {metric}={value} at {datetime.now().strftime('%H:%M:%S')}"


def query_recent_logs(service: str) -> str:
    """Query recent error logs for a service."""
    return f"""Recent logs for {service}:
ERROR: Anomaly detected at {datetime.now().strftime('%H:%M:%S')}
WARN: Error rate elevated above threshold
INFO: Health check failing on port 8080"""


def check_recent_deployments(service: str) -> str:
    """Check recent deployments."""
    return f"""Deployments for {service}:
- v2.3.1 deployed 4 minutes ago (connection pool config changed)
- v2.3.0 deployed 2 days ago (stable)"""


def get_service_dependencies(service: str) -> str:
    """Get service dependencies."""
    deps = {
        "payment-service": ["postgres", "redis", "auth-service"],
        "order-service": ["payment-service", "inventory-service"],
        "auth-service": ["redis", "postgres"],
    }
    service_deps = deps.get(service, ["postgres", "redis"])
    return f"{service} depends on: {', '.join(service_deps)}"


# ── METRIC MAPPING per incident type ─────────────────────────────────────────

INCIDENT_METRICS = {
    "database_connection_exhaustion": "db_connections",
    "memory_leak": "memory_usage",
    "high_latency_spike": "latency_p99",
    "cpu_throttling": "cpu_usage",
    "pod_crash_loop": "error_rate",
    "disk_space_critical": "error_rate",
    "network_partition": "error_rate",
    "deployment_failure": "error_rate",
    "cascade_failure": "error_rate",
    "unknown": "error_rate",
}


# ── INVESTIGATOR AGENT ────────────────────────────────────────────────────────

def investigator_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("🔎 INVESTIGATOR AGENT")
    print("="*50)
    print(f"Investigating: {state['incident_type']} on {state['service']}")

    service = state['service']
    incident_type = state.get('incident_type', 'unknown')
    evidence = list(state.get('evidence_chain', []))

    # Step 1 — Query most relevant metric
    metric = INCIDENT_METRICS.get(incident_type, "error_rate")
    print(f"\n   🔧 Querying Prometheus: {metric}")
    prometheus_result = query_prometheus(service, metric)
    print(f"   📊 {prometheus_result}")
    evidence.append(f"[INVESTIGATOR] Prometheus: {prometheus_result}")

    # Step 2 — Query recent logs
    print(f"\n   🔧 Querying recent logs...")
    log_result = query_recent_logs(service)
    print(f"   📊 {log_result[:80]}...")
    evidence.append(f"[INVESTIGATOR] Logs: {log_result[:150]}")

    # Step 3 — Check deployments
    print(f"\n   🔧 Checking recent deployments...")
    deploy_result = check_recent_deployments(service)
    print(f"   📊 {deploy_result[:80]}...")
    evidence.append(f"[INVESTIGATOR] Deployments: {deploy_result[:150]}")

    # Step 4 — Check dependencies
    print(f"\n   🔧 Checking service dependencies...")
    deps_result = get_service_dependencies(service)
    print(f"   📊 {deps_result}")
    evidence.append(f"[INVESTIGATOR] Dependencies: {deps_result}")

    # Step 5 — Use phi3:mini to summarize root cause
    print(f"\n   🧠 Summarizing root cause with phi3:mini...")
    llm = ChatOllama(model="phi3:mini", temperature=0)

    evidence_text = "\n".join(evidence)
    prompt = f"""Based on this evidence, what is the root cause of this incident?
Incident: {incident_type} on {service}
Evidence:
{evidence_text}

Respond in ONE sentence starting with: "Root cause is..."
"""
    response = llm.invoke([HumanMessage(content=prompt)])
    root_cause = response.content.strip()[:200]

    print(f"\n✅ Investigation Complete:")
    print(f"   Root Cause: {root_cause}")
    print(f"   Evidence points: {len(evidence)}")

    return {
        **state,
        "evidence_chain": evidence,
        "root_cause": root_cause,
    }
