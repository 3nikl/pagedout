"""
PagedOut - Runbook RAG Agent
Finds the right runbook for the incident.
For now uses a local runbook dictionary.
Later replaced with Qdrant hybrid search.
"""

from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage


# ── LOCAL RUNBOOK KNOWLEDGE BASE (replaced by Qdrant in Phase 5) ─────────────

RUNBOOKS = {
    "database_connection_exhaustion": {
        "title": "DB Connection Pool Recovery",
        "steps": [
            "SAFE: Restart connection pool manager",
            "SAFE: Clear idle connections older than 30 seconds",
            "SAFE: Scale up connection pool size temporarily",
            "RISKY: Rollback recent deployment if correlation found",
            "RISKY: Restart database if pool restart fails",
        ]
    },
    "memory_leak": {
        "title": "Memory Leak Response",
        "steps": [
            "SAFE: Restart affected pods to clear memory",
            "SAFE: Scale up replica count to distribute load",
            "RISKY: Enable heap dump for analysis",
            "RISKY: Rollback deployment if memory grew after deploy",
        ]
    },
    "high_latency_spike": {
        "title": "High Latency Response",
        "steps": [
            "SAFE: Check downstream service health",
            "SAFE: Clear cache if stale data suspected",
            "SAFE: Scale up replicas to reduce per-instance load",
            "RISKY: Enable circuit breaker to protect upstream",
            "RISKY: Rollback if latency started after deployment",
        ]
    },
    "pod_crash_loop": {
        "title": "Pod CrashLoopBackOff Recovery",
        "steps": [
            "SAFE: Check pod logs for crash reason",
            "SAFE: Delete and recreate crashing pod",
            "SAFE: Increase memory limits if OOMKilled",
            "RISKY: Rollback deployment causing crashes",
        ]
    },
    "disk_space_critical": {
        "title": "Disk Space Recovery",
        "steps": [
            "SAFE: Clear old log files older than 7 days",
            "SAFE: Clear Docker unused images and volumes",
            "SAFE: Archive old database partitions",
            "RISKY: Expand disk volume",
        ]
    },
    "network_partition": {
        "title": "Network Partition Response",
        "steps": [
            "SAFE: Verify network connectivity between services",
            "SAFE: Check DNS resolution",
            "SAFE: Restart network proxy/sidecar",
            "RISKY: Failover to backup region",
        ]
    },
    "cpu_throttling": {
        "title": "CPU Throttling Response",
        "steps": [
            "SAFE: Scale up replica count",
            "SAFE: Increase CPU limits",
            "SAFE: Check for runaway processes",
            "RISKY: Rollback if throttling started after deployment",
        ]
    },
    "deployment_failure": {
        "title": "Deployment Failure Response",
        "steps": [
            "SAFE: Check deployment logs",
            "SAFE: Verify new pods are healthy",
            "RISKY: Rollback to previous version",
            "RISKY: Scale down failed deployment",
        ]
    },
    "unknown": {
        "title": "General Incident Response",
        "steps": [
            "SAFE: Gather logs and metrics",
            "SAFE: Check recent deployments",
            "RISKY: Escalate to senior engineer",
        ]
    }
}


def runbook_rag_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("📚 RUNBOOK RAG AGENT")
    print("="*50)
    print(f"Searching runbooks for: {state['incident_type']}")

    incident_type = state.get('incident_type', 'unknown')
    runbook = RUNBOOKS.get(incident_type, RUNBOOKS['unknown'])

    print(f"\n✅ Runbook Found: {runbook['title']}")
    print(f"   Steps: {len(runbook['steps'])}")
    for step in runbook['steps']:
        print(f"   - {step}")

    return {
        **state,
        "matched_runbook": runbook['title'],
        "remediation_steps": runbook['steps'],
        "evidence_chain": state.get('evidence_chain', []) + [
            f"[RUNBOOK] Found: {runbook['title']} with {len(runbook['steps'])} steps"
        ]
    }
