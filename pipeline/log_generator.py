"""
PagedOut synthetic telemetry generator.

Produces correlated incident signals across three Kafka topics. A single
"incident" fans out into many log lines, a metric sample and an alert, all
sharing a service and incident_type — which is exactly the fan-out the Flink
correlation job has to collapse back into one incident.

Uses confluent-kafka (librdkafka) rather than kafka-python. At the rates the
load harness drives, a pure-Python producer becomes the bottleneck and the
benchmark ends up measuring the generator instead of the pipeline.

Usage:
    python pipeline/log_generator.py                      # 50 events/sec, forever
    python pipeline/log_generator.py --rate 2000 --duration 30
    python pipeline/log_generator.py --rate 500 --services 4
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from confluent_kafka import Producer

# ── Config ────────────────────────────────────────────────────────────────────

BOOTSTRAP = "localhost:9092"

TOPIC_LOGS = "logs.raw"
TOPIC_METRICS = "metrics.raw"
TOPIC_ALERTS = "alerts.raw"

# These match the victim app's service names so the generator and the live
# app describe the same world.
SERVICES = [
    "checkout-service",
    "payment-service",
    "ledger-service",
    "auth-service",
    "order-service",
    "inventory-service",
    "notification-service",
    "search-service",
    "billing-service",
    "api-gateway",
]


def iso_now() -> str:
    """ISO-8601 with milliseconds and a literal Z.

    Flink's `json.timestamp-format.standard = ISO-8601` parser wants exactly
    this shape. Python's default isoformat() emits microseconds and a
    +00:00 offset, which Flink rejects.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ── Incident templates ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IncidentTemplate:
    type: str
    severity: str
    log_messages: tuple[str, ...]
    metrics: dict[str, tuple[float, float]] = field(default_factory=dict)
    runbook_hint: str = ""


TEMPLATES: tuple[IncidentTemplate, ...] = (
    IncidentTemplate(
        type="database_connection_exhaustion",
        severity="P1",
        log_messages=(
            "FATAL: Connection pool exhausted. Active connections: {value}/100",
            "ERROR: Unable to acquire database connection after 30s timeout",
            "WARN: Connection wait time exceeding threshold: {value}ms",
            "ERROR: HikariPool-1 - Connection is not available, request timed out",
        ),
        metrics={
            "db_connections": (95, 100),
            "error_rate": (0.3, 0.9),
            "latency_p99": (2000, 8000),
        },
        runbook_hint="connection pool",
    ),
    IncidentTemplate(
        type="memory_leak",
        severity="P2",
        log_messages=(
            "WARN: Heap usage at {value}% - approaching OOM threshold",
            "ERROR: GC overhead limit exceeded",
            "INFO: Full GC triggered, pause time: {value}ms",
            "ERROR: java.lang.OutOfMemoryError: Java heap space",
        ),
        metrics={
            "memory_usage": (85, 99),
            "gc_pause_ms": (500, 3000),
            "error_rate": (0.05, 0.4),
        },
        runbook_hint="heap memory",
    ),
    IncidentTemplate(
        type="high_latency_spike",
        severity="P2",
        log_messages=(
            "WARN: Request latency p99 at {value}ms, SLA is 500ms",
            "ERROR: Upstream timeout after {value}ms",
            "WARN: Thread pool saturated, queue depth {value}",
        ),
        metrics={
            "latency_p99": (1000, 5000),
            "timeout_rate": (0.1, 0.4),
            "error_rate": (0.05, 0.2),
        },
        runbook_hint="latency",
    ),
    IncidentTemplate(
        type="pod_crash_loop",
        severity="P1",
        log_messages=(
            "ERROR: Back-off restarting failed container, restart count {value}",
            "FATAL: Liveness probe failed: HTTP 500",
            "ERROR: Container terminated with exit code 137 (OOMKilled)",
        ),
        metrics={
            "pod_restarts": (5, 20),
            "availability": (0.3, 0.7),
            "error_rate": (0.5, 0.95),
        },
        runbook_hint="crash loop",
    ),
    IncidentTemplate(
        type="disk_space_critical",
        severity="P1",
        log_messages=(
            "ERROR: No space left on device",
            "WARN: Log rotation failed, disk at {value}%",
            "FATAL: Cannot write WAL segment, filesystem full",
        ),
        metrics={
            "disk_usage": (92, 99),
            "write_errors": (10, 100),
            "iops": (0.1, 0.3),
        },
        runbook_hint="disk space",
    ),
    IncidentTemplate(
        type="network_partition",
        severity="P2",
        log_messages=(
            "ERROR: Failed to connect to peer: Connection refused",
            "WARN: Service discovery returning stale endpoints",
            "ERROR: gRPC stream disconnected, reconnecting attempt {value}",
        ),
        metrics={
            "network_errors": (50, 200),
            "packet_loss": (0.1, 0.4),
            "latency_p99": (800, 3000),
        },
        runbook_hint="network",
    ),
    IncidentTemplate(
        type="cpu_throttling",
        severity="P3",
        log_messages=(
            "WARN: CPU throttling detected, throttled time {value}%",
            "INFO: Thread pool queue depth {value}, consider scaling",
            "WARN: Request processing degraded by {value}%",
        ),
        metrics={
            "cpu_throttle_pct": (30, 80),
            "thread_queue_depth": (50, 200),
            "latency_p95": (400, 1200),
        },
        runbook_hint="cpu throttling",
    ),
    IncidentTemplate(
        type="deployment_failure",
        severity="P1",
        log_messages=(
            "ERROR: NullPointerException in OrderValidator after deploy",
            "ERROR: Readiness probe failing for new ReplicaSet",
            "WARN: Error rate jumped {value}% following release",
        ),
        metrics={
            "error_rate": (0.4, 0.95),
            "rollout_progress": (0.1, 0.6),
            "latency_p99": (1500, 6000),
        },
        runbook_hint="bad deploy",
    ),
)


# ── Generator ─────────────────────────────────────────────────────────────────


class IncidentGenerator:
    """Emits bursts of correlated signals.

    One call to `emit_incident` produces `fanout` log events, one metric
    event and one alert event, all tagged with the same service and
    incident_type. The Flink job should turn that entire burst into a single
    correlated incident.
    """

    def __init__(self, bootstrap: str = BOOTSTRAP, services: int = len(SERVICES)):
        self.producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "linger.ms": 5,
                "batch.size": 262144,
                "compression.type": "lz4",
                "acks": 1,
                "queue.buffering.max.messages": 1_000_000,
                "queue.buffering.max.kbytes": 512_000,
            }
        )
        self.services = SERVICES[:services]
        self.sent = 0

    def _send(self, topic: str, key: str, payload: dict) -> None:
        payload["emitted_at_ms"] = int(time.time() * 1000)
        while True:
            try:
                self.producer.produce(
                    topic, key=key, value=json.dumps(payload).encode()
                )
                break
            except BufferError:
                # Local queue is full; let librdkafka drain before retrying.
                self.producer.poll(0.1)
        self.sent += 1

    def emit_incident(self, fanout: int = 4) -> int:
        """Emit one correlated burst. Returns the number of events produced."""
        tpl = random.choice(TEMPLATES)
        service = random.choice(self.services)
        ts = iso_now()
        pod = f"{service}-{uuid.uuid4().hex[:8]}"

        for _ in range(fanout):
            template = random.choice(tpl.log_messages)
            message = template.replace("{value}", str(random.randint(50, 9000)))
            self._send(
                TOPIC_LOGS,
                service,
                {
                    "event_id": str(uuid.uuid4()),
                    "timestamp": ts,
                    "service": service,
                    "incident_type": tpl.type,
                    "severity": tpl.severity,
                    "message": message,
                    "pod": pod,
                    "namespace": "production",
                },
            )

        self._send(
            TOPIC_METRICS,
            service,
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": ts,
                "service": service,
                "incident_type": tpl.type,
                "severity": tpl.severity,
                "metrics": {
                    name: round(random.uniform(lo, hi), 3)
                    for name, (lo, hi) in tpl.metrics.items()
                },
            },
        )

        self._send(
            TOPIC_ALERTS,
            service,
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": ts,
                "service": service,
                "incident_type": tpl.type,
                "severity": tpl.severity,
                "title": f"[{tpl.severity}] {tpl.type} - {service}",
                "description": f"Detected {tpl.type.replace('_', ' ')} on {service}",
                "runbook_hint": tpl.runbook_hint,
            },
        )

        return fanout + 2

    def poll(self, timeout: float = 0.0) -> None:
        self.producer.poll(timeout)

    def flush(self, timeout: float = 30.0) -> int:
        return self.producer.flush(timeout)


# ── CLI ───────────────────────────────────────────────────────────────────────


def run(rate: int, duration: float | None, fanout: int, services: int) -> None:
    gen = IncidentGenerator(services=services)
    stopping = {"flag": False}

    def _stop(*_):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    events_per_incident = fanout + 2
    incidents_per_sec = max(rate / events_per_incident, 0.1)
    interval = 1.0 / incidents_per_sec

    print(
        f"Producing ~{rate} events/sec "
        f"({incidents_per_sec:.1f} incidents/sec x {events_per_incident} events) "
        f"across {len(gen.services)} services -> {BOOTSTRAP}"
    )
    if duration:
        print(f"Duration: {duration}s")
    print("Ctrl-C to stop.\n")

    start = time.perf_counter()
    next_emit = start
    last_report = start
    last_sent = 0

    while not stopping["flag"]:
        now = time.perf_counter()
        if duration and now - start >= duration:
            break

        if now >= next_emit:
            gen.emit_incident(fanout)
            next_emit += interval
            # If we have fallen behind, do not try to catch up in a burst.
            if next_emit < now:
                next_emit = now + interval
        else:
            gen.poll(0)
            time.sleep(min(interval / 4, 0.002))

        if now - last_report >= 5.0:
            delta = gen.sent - last_sent
            print(
                f"  [{now - start:6.1f}s] sent={gen.sent:>8,}  "
                f"rate={delta / (now - last_report):>8,.0f} ev/s"
            )
            last_report, last_sent = now, gen.sent

    remaining = gen.flush()
    elapsed = time.perf_counter() - start
    print(
        f"\nDone. {gen.sent:,} events in {elapsed:.1f}s "
        f"({gen.sent / elapsed:,.0f} ev/s average)."
    )
    if remaining:
        print(f"WARNING: {remaining} messages still queued at exit.")


def main() -> None:
    p = argparse.ArgumentParser(description="PagedOut telemetry generator")
    p.add_argument("--rate", type=int, default=50, help="target events/sec")
    p.add_argument("--duration", type=float, default=None, help="seconds to run")
    p.add_argument("--fanout", type=int, default=4, help="log events per incident")
    p.add_argument(
        "--services", type=int, default=len(SERVICES), help="distinct services"
    )
    args = p.parse_args()
    run(args.rate, args.duration, args.fanout, args.services)


if __name__ == "__main__":
    main()
