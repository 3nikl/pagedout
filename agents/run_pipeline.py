"""
Run the agent pipeline against a live incident.

Three modes:

  --live      break a real victim service, read the resulting incident from
              the Flink `incidents.correlated` topic, and run the graph on
              it. This is the full loop: real fault -> real telemetry ->
              Kafka -> Flink correlation -> agents.

  --kafka     wait for whatever incident Flink emits next and process that.

  (default)   run a synthetic incident through the graph. Useful when the
              stack is not up.

Usage:
    python agents/run_pipeline.py --live --fault pool_exhaustion --service ledger-service
    python agents/run_pipeline.py --kafka --timeout 120
    python agents/run_pipeline.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools import SERVICE_PORTS  # noqa: E402

KAFKA_BOOTSTRAP = "localhost:9092"
INCIDENTS_TOPIC = "incidents.correlated"


def blank_state(**overrides) -> dict:
    """A fully populated state dict.

    Every key is present with a typed empty value. LangGraph merges partial
    updates, but nodes read fields that earlier nodes may not have set, and
    a missing key is a KeyError three nodes later.
    """
    base = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "service": "",
        "severity": "P2",
        "raw_logs": [],
        "raw_metrics": {},
        "alert_title": "",
        "incident_type": "",
        "confidence": 0.0,
        "triage_summary": "",
        "next_agent": "",
        "evidence_chain": [],
        "evidence_values": [],
        "root_cause": "",
        "cascade_origin": "",
        "matched_runbook": "",
        "remediation_steps": [],
        "retrieved_sources": [],
        "retrieval_ms": 0.0,
        "plan": {},
        "remediation_plan_valid": False,
        "planner_attempts": [],
        "actions_taken": [],
        "actions_pending": [],
        "postmortem": "",
        "messages": [],
    }
    base.update(overrides)
    return base


# ── Live incident generation ──────────────────────────────────────────────────


def inject_fault(service: str, fault: str) -> None:
    port = SERVICE_PORTS[service]
    req = urllib.request.Request(
        f"http://localhost:{port}/chaos/{fault}", method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    print(f"   injected {fault} into {service}")


def clear_faults() -> None:
    for service, port in SERVICE_PORTS.items():
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/chaos/clear", method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        except OSError:
            pass


def drive_traffic(service: str, count: int = 30) -> None:
    """Generate requests so the fault produces real logs and metrics."""
    port = SERVICE_PORTS[service]
    ok = err = 0
    for _ in range(count):
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/api/process", timeout=5
            ) as r:
                r.read()
                ok += 1
        except Exception:
            err += 1
    print(f"   drove {count} requests: {ok} ok, {err} failed")


def wait_for_incident(timeout: float, service: str | None = None) -> dict | None:
    """Read the next correlated incident Flink emits."""
    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": f"agent-runner-{time.time()}",
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([INCIDENTS_TOPIC])

    print(f"   waiting up to {timeout:.0f}s for an incident on {INCIDENTS_TOPIC}...")
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            incident = json.loads(msg.value())
            if service and incident.get("service") != service:
                continue
            return incident
    finally:
        consumer.close()
    return None


def state_from_incident(incident: dict) -> dict:
    """Map a Flink correlated-incident row onto agent state."""
    service = incident.get("service", "unknown")
    itype = incident.get("incident_type", "unknown")
    severity = incident.get("severity", "P2")
    return blank_state(
        service=service,
        severity=severity,
        alert_title=f"[{severity}] {itype} - {service}",
        raw_logs=[incident.get("sample_message", "")],
        raw_metrics={
            "signal_count": incident.get("signal_count", 0),
            "log_count": incident.get("log_count", 0),
            "metric_count": incident.get("metric_count", 0),
            "alert_count": incident.get("alert_count", 0),
            "distinct_pods": incident.get("distinct_pods", 0),
        },
    )


SYNTHETIC = blank_state(
    service="ledger-service",
    severity="P1",
    alert_title="[P1] database_connection_exhaustion - ledger-service",
    raw_logs=[
        "FATAL: Connection pool exhausted. Active connections: 20/20",
        "ERROR: Unable to acquire database connection after 30s timeout",
        "WARN: Connection wait time exceeding threshold: 4500ms",
    ],
    raw_metrics={"db_connections": 20, "pool_max": 20, "error_rate": 1.0},
)


# ── Runner ────────────────────────────────────────────────────────────────────


def run(state: dict, use_postgres: bool) -> dict:
    from graph import checkpointed_app, memory_app

    thread_id = state["event_id"]
    config = {"configurable": {"thread_id": thread_id}}

    print("\n" + "█" * 64)
    print(f"🚨 INCIDENT {thread_id[:8]}  {state['severity']}  {state['service']}")
    print(f"   {state['alert_title']}")
    print("█" * 64)

    start = time.perf_counter()

    if use_postgres:
        with checkpointed_app() as app:
            result = app.invoke(state, config=config)
            checkpoints = list(app.checkpointer.list(config))
    else:
        app = memory_app()
        result = app.invoke(state, config=config)
        checkpoints = list(app.checkpointer.list(config))

    elapsed = time.perf_counter() - start

    print("\n" + "=" * 64)
    print("📊 PIPELINE COMPLETE")
    print("=" * 64)
    print(f"  wall clock        : {elapsed:.1f}s")
    print(f"  incident type     : {result.get('incident_type')} "
          f"({result.get('confidence', 0):.0%} confidence)")
    print(f"  root cause        : {result.get('root_cause', '')[:100]}")
    if result.get("cascade_origin"):
        print(f"  cascade origin    : {result['cascade_origin']}")
    print(f"  runbook           : {result.get('matched_runbook') or '(none)'} "
          f"({result.get('retrieval_ms', 0):.0f}ms)")
    plan = result.get("plan") or {}
    print(f"  plan actions      : {len(plan.get('actions', []))} "
          f"(approval required: {plan.get('requires_approval')})")
    print(f"  auto-executable   : {len(result.get('actions_taken', []))}")
    print(f"  awaiting approval : {len(result.get('actions_pending', []))}")
    print(f"  checkpoints saved : {len(checkpoints)} "
          f"({'PostgreSQL' if use_postgres else 'in-memory'})")

    print(f"\n  evidence chain ({len(result.get('evidence_chain', []))} entries):")
    for line in result.get("evidence_chain", []):
        print(f"    {line[:110]}")

    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="inject a real fault first")
    ap.add_argument("--kafka", action="store_true", help="consume the next incident")
    ap.add_argument("--service", default="ledger-service")
    ap.add_argument("--fault", default="pool_exhaustion")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-postgres", action="store_true",
                    help="use the in-memory checkpointer instead")
    args = ap.parse_args()

    if args.live:
        print("=== LIVE MODE ===")
        clear_faults()
        inject_fault(args.service, args.fault)
        time.sleep(6)
        drive_traffic(args.service)
        incident = wait_for_incident(args.timeout, service=args.service)
        if incident is None:
            print("   no correlated incident arrived; falling back to a direct state")
            state = blank_state(
                service=args.service,
                severity="P1",
                alert_title=f"[P1] {args.fault} - {args.service}",
                raw_logs=[f"fault {args.fault} active on {args.service}"],
            )
        else:
            print(f"   got incident: {incident['service']} / "
                  f"{incident['incident_type']} ({incident['signal_count']} signals)")
            state = state_from_incident(incident)
    elif args.kafka:
        incident = wait_for_incident(args.timeout)
        if incident is None:
            print("no incident arrived within the timeout")
            return
        state = state_from_incident(incident)
    else:
        state = SYNTHETIC

    run(state, use_postgres=not args.no_postgres)


if __name__ == "__main__":
    main()
