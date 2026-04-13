"""
PagedOut - Synthetic Log and Metrics Generator
Generates realistic microservice incident telemetry for testing the agent pipeline.
"""

import json
import random
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, asdict

from kafka import KafkaProducer


# ── Config ──────────────────────────────────────────────────────────────────

KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
TOPIC_LOGS = "logs.raw"
TOPIC_METRICS = "metrics.raw"
TOPIC_ALERTS = "alerts.raw"

SERVICES = [
    "payment-service",
    "auth-service",
    "order-service",
    "inventory-service",
    "notification-service",
    "user-service",
    "search-service",
    "recommendation-service",
    "billing-service",
    "api-gateway",
]


# ── Severity ─────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    P1 = "P1"  # Critical — immediate action required
    P2 = "P2"  # High — action required within 30 mins
    P3 = "P3"  # Medium — action required within 2 hours


# ── Incident Templates ────────────────────────────────────────────────────────

INCIDENT_TEMPLATES = [
    {
        "type": "database_connection_exhaustion",
        "severity": Severity.P1,
        "log_messages": [
            "FATAL: Connection pool exhausted. Active connections: {value}/100",
            "ERROR: Unable to acquire database connection after 30s timeout",
            "WARN: Connection wait time exceeding threshold: {value}ms",
        ],
        "metrics": {"db_connections": (95, 100), "error_rate": (0.3, 0.9), "latency_p99": (2000, 8000)},
    },
    {
        "type": "memory_leak",
        "severity": Severity.P2,
        "log_messages": [
            "WARN: Heap usage at {value}% - approaching OOM threshold",
            "ERROR: GC overhead limit exceeded",
            "INFO: Full GC triggered, pause time: {value}ms",
        ],
        "metrics": {"memory_usage": (85, 99), "gc_pause_ms": (500, 3000), "request_rate": (0.4, 0.7)},
    },
    {
        "type": "high_latency_spike",
        "severity": Severity.P2,
        "log_messages": [
            "WARN: Request latency p99 exceeds SLA: {value}ms (threshold: 500ms)",
            "ERROR: Downstream service timeout after {value}ms",
            "WARN: Circuit breaker half-open, retry count: {value}",
        ],
        "metrics": {"latency_p99": (1000, 5000), "timeout_rate": (0.1, 0.4), "error_rate": (0.05, 0.2)},
    },
    {
        "type": "pod_crash_loop",
        "severity": Severity.P1,
        "log_messages": [
            "FATAL: Pod {pod} entered CrashLoopBackOff state",
            "ERROR: Container exited with code 137 (OOMKilled)",
            "WARN: Restart count: {value} in last 10 minutes",
        ],
        "metrics": {"pod_restarts": (5, 20), "availability": (0.3, 0.7), "error_rate": (0.5, 0.95)},
    },
    {
        "type": "disk_space_critical",
        "severity": Severity.P1,
        "log_messages": [
            "FATAL: Disk usage at {value}% on /var/log — write operations failing",
            "ERROR: No space left on device",
            "WARN: Log rotation failed, disk at {value}%",
        ],
        "metrics": {"disk_usage": (92, 99), "write_errors": (10, 100), "iops": (0.1, 0.3)},
    },
    {
        "type": "network_partition",
        "severity": Severity.P2,
        "log_messages": [
            "ERROR: Failed to connect to {service}: Connection refused",
            "WARN: Service discovery returning stale endpoints for {service}",
            "ERROR: gRPC stream disconnected, reconnecting... attempt {value}",
        ],
        "metrics": {"network_errors": (50, 200), "packet_loss": (0.1, 0.4), "latency_p99": (800, 3000)},
    },
    {
        "type": "cpu_throttling",
        "severity": Severity.P3,
        "log_messages": [
            "WARN: CPU throttling detected, throttled time: {value}%",
            "INFO: Thread pool queue depth: {value} — consider scaling",
            "WARN: Request processing time degraded by {value}%",
        ],
        "metrics": {"cpu_throttle_pct": (30, 80), "thread_queue_depth": (50, 200), "latency_p95": (400, 1200)},
    },
]


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class LogEvent:
    event_id: str
    timestamp: str
    service: str
    incident_type: str
    severity: str
    message: str
    pod: str
    namespace: str = "production"


@dataclass
class MetricEvent:
    event_id: str
    timestamp: str
    service: str
    incident_type: str
    severity: str
    metrics: dict


@dataclass
class AlertEvent:
    event_id: str
    timestamp: str
    service: str
    incident_type: str
    severity: str
    title: str
    description: str
    runbook_hint: str


# ── Generator ─────────────────────────────────────────────────────────────────

class IncidentGenerator:
    def __init__(self):
        self.producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8"),
        )
        print(f"Connected to Kafka at {KAFKA_BOOTSTRAP_SERVERS}")

    def _random_value(self, metric_range: tuple) -> float:
        lo, hi = metric_range
        return round(random.uniform(lo, hi), 2)

    def _generate_incident(self) -> dict:
        template = random.choice(INCIDENT_TEMPLATES)
        service = random.choice(SERVICES)
        event_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        pod = f"{service}-{uuid.uuid4().hex[:8]}"

        # Build log message
        msg_template = random.choice(template["log_messages"])
        message = msg_template.format(
            value=self._random_value((1, 9999)),
            service=random.choice(SERVICES),
            pod=pod,
        )

        # Build metrics
        metrics = {
            k: self._random_value(v)
            for k, v in template["metrics"].items()
        }

        return {
            "event_id": event_id,
            "timestamp": timestamp,
            "service": service,
            "incident_type": template["type"],
            "severity": template["severity"].value,
            "message": message,
            "pod": pod,
            "metrics": metrics,
        }

    def emit_log(self, incident: dict):
        log = LogEvent(
            event_id=incident["event_id"],
            timestamp=incident["timestamp"],
            service=incident["service"],
            incident_type=incident["incident_type"],
            severity=incident["severity"],
            message=incident["message"],
            pod=incident["pod"],
        )
        self.producer.send(TOPIC_LOGS, key=incident["service"], value=asdict(log))

    def emit_metric(self, incident: dict):
        metric = MetricEvent(
            event_id=incident["event_id"],
            timestamp=incident["timestamp"],
            service=incident["service"],
            incident_type=incident["incident_type"],
            severity=incident["severity"],
            metrics=incident["metrics"],
        )
        self.producer.send(TOPIC_METRICS, key=incident["service"], value=asdict(metric))

    def emit_alert(self, incident: dict):
        alert = AlertEvent(
            event_id=incident["event_id"],
            timestamp=incident["timestamp"],
            service=incident["service"],
            incident_type=incident["incident_type"],
            severity=incident["severity"],
            title=f"[{incident['severity']}] {incident['incident_type'].replace('_', ' ').title()} - {incident['service']}",
            description=incident["message"],
            runbook_hint=f"runbook/{incident['incident_type']}",
        )
        self.producer.send(TOPIC_ALERTS, key=incident["service"], value=asdict(alert))

    def run(self, interval_seconds: float = 2.0, max_events: Optional[int] = None):
        print(f"Generating incidents every {interval_seconds}s...")
        count = 0
        try:
            while True:
                incident = self._generate_incident()
                self.emit_log(incident)
                self.emit_metric(incident)

                # Only emit alert for P1 and P2
                if incident["severity"] in (Severity.P1.value, Severity.P2.value):
                    self.emit_alert(incident)

                self.producer.flush()
                count += 1
                print(f"[{count}] {incident['severity']} | {incident['service']} | {incident['incident_type']}")

                if max_events and count >= max_events:
                    print(f"Generated {count} events. Done.")
                    break

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            self.producer.close()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    generator = IncidentGenerator()
    generator.run(interval_seconds=2.0)
