"""
PagedOut - Main Pipeline Runner
Run this to test the complete 5-agent pipeline.

Usage:
    python agents/run_pipeline.py
"""

import sys
import os
import time
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from graph import app


# ── TEST INCIDENTS ────────────────────────────────────────────────────────────

TEST_INCIDENTS = [
    {
        "name": "P1 Database Connection Exhaustion",
        "state": {
            "event_id": "test-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "payment-service",
            "severity": "P1",
            "raw_logs": [
                "FATAL: Connection pool exhausted. Active connections: 99/100",
                "ERROR: Unable to acquire database connection after 30s timeout",
                "WARN: Connection wait time exceeding threshold: 4500ms",
            ],
            "raw_metrics": {
                "db_connections": 99,
                "error_rate": 0.78,
                "latency_p99": 6500,
            },
            "alert_title": "[P1] database_connection_exhaustion - payment-service",
            "incident_type": "",
            "confidence": 0.0,
            "triage_summary": "",
            "next_agent": "",
            "evidence_chain": [],
            "root_cause": "",
            "matched_runbook": "",
            "remediation_steps": [],
            "actions_taken": [],
            "actions_pending": [],
            "postmortem": "",
            "messages": [],
        }
    },
    {
        "name": "P2 Memory Leak",
        "state": {
            "event_id": "test-002",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "order-service",
            "severity": "P2",
            "raw_logs": [
                "WARN: Heap usage at 94% - approaching OOM threshold",
                "ERROR: GC overhead limit exceeded",
                "INFO: Full GC triggered, pause time: 2300ms",
            ],
            "raw_metrics": {
                "memory_usage": 94,
                "gc_pause_ms": 2300,
                "error_rate": 0.12,
            },
            "alert_title": "[P2] memory_leak - order-service",
            "incident_type": "",
            "confidence": 0.0,
            "triage_summary": "",
            "next_agent": "",
            "evidence_chain": [],
            "root_cause": "",
            "matched_runbook": "",
            "remediation_steps": [],
            "actions_taken": [],
            "actions_pending": [],
            "postmortem": "",
            "messages": [],
        }
    },
]


# ── MAIN RUNNER ───────────────────────────────────────────────────────────────

def run_incident(incident: dict):
    print("\n" + "█"*60)
    print(f"🚨 INCIDENT: {incident['name']}")
    print("█"*60)

    start_time = time.time()

    # Run through all 5 agents
    result = app.invoke(incident['state'])

    elapsed = time.time() - start_time

    # Final summary
    print("\n" + "="*60)
    print("📊 PIPELINE COMPLETE")
    print("="*60)
    print(f"⏱️  Total time: {elapsed:.1f} seconds")
    print(f"🏷️  Incident type: {result['incident_type']}")
    print(f"🎯 Confidence: {result['confidence']:.0%}")
    print(f"🔍 Root cause: {result['root_cause'][:100]}")
    print(f"✅ Actions taken: {len(result['actions_taken'])}")
    print(f"⏳ Pending approval: {len(result['actions_pending'])}")
    print(f"\n📋 Evidence Chain:")
    for e in result['evidence_chain']:
        print(f"   {e}")

    return result


if __name__ == "__main__":
    print("\n" + "🚀"*20)
    print("PAGEDOUT - AUTONOMOUS SRE INCIDENT RESPONSE")
    print("🚀"*20)
    print("\nRunning test incidents through 5-agent pipeline...")
    print("Model: phi3:mini via Ollama (local, free)")

    # Run the first test incident
    result = run_incident(TEST_INCIDENTS[0])

    print("\n\n✨ Phase 4 Complete — All 5 agents working!")
    print("Next: Phase 5 — Replace local runbooks with Qdrant RAG")
