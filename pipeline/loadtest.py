"""
PagedOut ingestion load test.

Drives the Kafka -> Flink pipeline at increasing rates and measures what
actually comes out the other end.

Design notes that matter for the numbers being trustworthy:

* Production runs in a separate PROCESS, not a thread. The consumer has to
  keep up while the producer saturates a core, and Python's GIL would make
  a threaded producer and consumer fight each other — the result would
  measure contention rather than the pipeline.

* Latency is measured end to end: the producer stamps `emitted_at_ms` before
  the message leaves, the consumer subtracts it from wall clock on receipt of
  the NORMALIZED record. So it covers Kafka produce + Flink processing +
  Kafka consume, which is the number worth reporting. Both processes run on
  the same host, so there is no clock skew.

* Each step reports achieved produce rate separately from target rate. If the
  producer cannot hit the target, the step is generator-bound, not
  pipeline-bound, and saying so is the difference between a real benchmark
  and a made-up one.

* The saturation point is where consumed throughput stops tracking produced
  throughput. That is the honest headline number.

Usage:
    python pipeline/loadtest.py                        # default ramp
    python pipeline/loadtest.py --rates 1000,5000,10000 --step-seconds 20
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import statistics
import time
from dataclasses import dataclass, asdict

from confluent_kafka import Consumer, Producer

from log_generator import TEMPLATES, SERVICES, iso_now

BOOTSTRAP = "localhost:9092"
OUTPUT_TOPIC = "signals.normalized"

DEFAULT_RATES = [500, 1_000, 2_000, 4_000, 8_000, 16_000]


# ── Producer process ──────────────────────────────────────────────────────────


def _producer_proc(rate: int, seconds: float, conn) -> None:
    """Produce at `rate` events/sec for `seconds`, then report what it managed.

    Emits in small batches rather than one message per sleep. At 16k events/sec
    the inter-message interval is 60 microseconds, far below the resolution of
    time.sleep(), so a per-message sleep loop would silently cap the rate at a
    few thousand per second and we would benchmark the sleep call.
    """
    import random
    import uuid

    producer = Producer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "linger.ms": 5,
            "batch.size": 1_048_576,
            "compression.type": "lz4",
            "acks": 1,
            "queue.buffering.max.messages": 2_000_000,
            "queue.buffering.max.kbytes": 1_048_576,
        }
    )

    sent = 0
    start = time.perf_counter()
    deadline = start + seconds

    while True:
        now = time.perf_counter()
        if now >= deadline:
            break

        # How many events *should* have been sent by now, minus what we have.
        target_so_far = int((now - start) * rate)
        deficit = target_so_far - sent

        if deficit <= 0:
            producer.poll(0)
            time.sleep(0.0005)
            continue

        batch = min(deficit, 2_000)
        emitted = 0
        while emitted < batch:
            tpl = random.choice(TEMPLATES)
            service = random.choice(SERVICES)
            ts = iso_now()
            pod = f"{service}-{uuid.uuid4().hex[:8]}"
            now_ms = int(time.time() * 1000)

            records = []
            for _ in range(4):
                msg = random.choice(tpl.log_messages).replace(
                    "{value}", str(random.randint(50, 9000))
                )
                records.append(
                    (
                        "logs.raw",
                        {
                            "event_id": str(uuid.uuid4()),
                            "timestamp": ts,
                            "service": service,
                            "incident_type": tpl.type,
                            "severity": tpl.severity,
                            "message": msg,
                            "pod": pod,
                            "namespace": "production",
                            "emitted_at_ms": now_ms,
                        },
                    )
                )
            records.append(
                (
                    "metrics.raw",
                    {
                        "event_id": str(uuid.uuid4()),
                        "timestamp": ts,
                        "service": service,
                        "incident_type": tpl.type,
                        "severity": tpl.severity,
                        "metrics": {
                            k: round(random.uniform(lo, hi), 3)
                            for k, (lo, hi) in tpl.metrics.items()
                        },
                        "emitted_at_ms": now_ms,
                    },
                )
            )
            records.append(
                (
                    "alerts.raw",
                    {
                        "event_id": str(uuid.uuid4()),
                        "timestamp": ts,
                        "service": service,
                        "incident_type": tpl.type,
                        "severity": tpl.severity,
                        "title": f"[{tpl.severity}] {tpl.type} - {service}",
                        "description": f"Detected {tpl.type} on {service}",
                        "runbook_hint": tpl.runbook_hint,
                        "emitted_at_ms": now_ms,
                    },
                )
            )

            for topic, payload in records:
                while True:
                    try:
                        producer.produce(
                            topic, key=service, value=json.dumps(payload).encode()
                        )
                        break
                    except BufferError:
                        producer.poll(0.05)
                sent += 1
                emitted += 1

        producer.poll(0)

    unflushed = producer.flush(30)
    elapsed = time.perf_counter() - start
    conn.send(
        {
            "target_rate": rate,
            "sent": sent,
            "elapsed": elapsed,
            "achieved_rate": sent / elapsed if elapsed else 0.0,
            "unflushed": unflushed,
        }
    )
    conn.close()


# ── Measurement ───────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    target_rate: int
    achieved_produce_rate: float
    consumed_rate: float
    consumed: int
    produced: int
    delivery_pct: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    flink_wrote: int
    generator_bound: bool
    pipeline_kept_up: bool


def output_topic_offsets(topic: str = OUTPUT_TOPIC) -> int:
    """Sum of high-watermark offsets across the output topic's partitions.

    Taking this before and after a step gives the number of records Flink
    actually WROTE, measured at the broker. That is independent of whether
    our own consumer managed to read them, which separates two very
    different failure modes: a saturated pipeline (Flink fell behind) versus
    a slow consumer (the benchmark fell behind).

    An earlier version read Flink's REST job-detail metrics instead. Those
    are cached and returned a stale cumulative figure, so the column was
    meaningless. Broker offsets cannot be stale.
    """
    from confluent_kafka import TopicPartition

    probe = Consumer(
        {"bootstrap.servers": BOOTSTRAP, "group.id": f"offsets-{time.time()}"}
    )
    try:
        md = probe.list_topics(topic, timeout=10)
        total = 0
        for part in md.topics[topic].partitions:
            _, high = probe.get_watermark_offsets(
                TopicPartition(topic, part), timeout=10, cached=False
            )
            total += high
        return total
    except Exception:
        return 0
    finally:
        probe.close()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def run_step(rate: int, seconds: float, drain_seconds: float) -> StepResult:
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": f"loadtest-{rate}-{int(time.time())}",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "fetch.min.bytes": 1,
            "queued.max.messages.kbytes": 512_000,
        }
    )
    consumer.subscribe([OUTPUT_TOPIC])
    # Force partition assignment before the producer starts, otherwise the
    # first seconds of output are missed and delivery looks artificially low.
    deadline = time.time() + 15
    while not consumer.assignment() and time.time() < deadline:
        consumer.poll(0.5)

    written_before = output_topic_offsets()

    parent, child = mp.Pipe()
    proc = mp.Process(target=_producer_proc, args=(rate, seconds, child), daemon=True)
    proc.start()

    latencies: list[float] = []
    consumed = 0
    t_start = time.perf_counter()
    stop_at = t_start + seconds + drain_seconds

    while time.perf_counter() < stop_at:
        # Short timeout deliberately. With a long one the consumer sleeps
        # between polls and records sit in the client buffer, so the measured
        # latency reflects poll cadence rather than pipeline latency — which
        # perversely makes low load look SLOWER than high load.
        msgs = consumer.consume(num_messages=1000, timeout=0.02)
        recv_ms = time.time() * 1000
        for m in msgs:
            if m is None or m.error():
                continue
            consumed += 1
            try:
                emitted = json.loads(m.value()).get("emitted_at_ms")
            except (ValueError, TypeError):
                continue
            if emitted:
                lat = recv_ms - emitted
                # Guard against records left over from a previous step.
                if 0 <= lat < 600_000:
                    latencies.append(lat)

    proc.join(timeout=10)
    pstats = parent.recv() if parent.poll() else {"sent": 0, "achieved_rate": 0.0}
    consumer.close()
    flink_wrote = max(0, output_topic_offsets() - written_before)

    produced = pstats.get("sent", 0)
    achieved = pstats.get("achieved_rate", 0.0)
    # Divide by the PRODUCE window, not the produce+drain window. The drain
    # exists only to collect stragglers; including it would understate
    # consumed throughput and make it incomparable to the produce rate.
    produce_secs = pstats.get("elapsed", seconds) or seconds
    consumed_rate = consumed / produce_secs if produce_secs else 0.0
    delivery = (consumed / produced * 100.0) if produced else 0.0

    return StepResult(
        target_rate=rate,
        achieved_produce_rate=achieved,
        consumed_rate=consumed_rate,
        consumed=consumed,
        produced=produced,
        delivery_pct=delivery,
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        p99_ms=percentile(latencies, 99),
        max_ms=max(latencies) if latencies else 0.0,
        flink_wrote=flink_wrote,
        # If the producer fell more than 15% short, this step tells us about
        # the generator, not the pipeline.
        generator_bound=achieved < rate * 0.85,
        # Healthy if nearly everything produced was also consumed.
        pipeline_kept_up=delivery >= 95.0,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="PagedOut ingestion load test")
    ap.add_argument("--rates", type=str, default=",".join(map(str, DEFAULT_RATES)))
    ap.add_argument("--step-seconds", type=float, default=20.0)
    ap.add_argument("--drain-seconds", type=float, default=8.0)
    ap.add_argument("--out", type=str, default="docs/benchmarks/loadtest_results.json")
    args = ap.parse_args()

    rates = [int(r) for r in args.rates.split(",") if r.strip()]

    print("=" * 96)
    print("PagedOut ingestion load test")
    print(f"  ramp: {rates}")
    print(f"  {args.step_seconds:.0f}s per step + {args.drain_seconds:.0f}s drain")
    print(f"  measuring end-to-end latency: produce -> Flink -> {OUTPUT_TOPIC}")
    print("=" * 96)
    print(
        f"\n{'target':>8} {'produced':>10} {'consumed':>10} {'flink_out':>10} "
        f"{'deliv%':>7} {'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'note':<20}"
    )
    print("-" * 96)

    results: list[StepResult] = []
    for rate in rates:
        r = run_step(rate, args.step_seconds, args.drain_seconds)
        results.append(r)
        note = ""
        if r.generator_bound:
            note = "GENERATOR-BOUND"
        elif not r.pipeline_kept_up:
            note = "PIPELINE SATURATED"
        print(
            f"{r.target_rate:>8,} {r.achieved_produce_rate:>10,.0f} "
            f"{r.consumed_rate:>10,.0f} {r.flink_wrote:>10,} "
            f"{r.delivery_pct:>6.1f}% "
            f"{r.p50_ms:>7,.0f} {r.p95_ms:>7,.0f} {r.p99_ms:>7,.0f} {note:<20}"
        )
        time.sleep(5)  # let the pipeline settle between steps

    print("-" * 96)

    sustained = [r for r in results if r.pipeline_kept_up and not r.generator_bound]
    print("\nSUMMARY")
    if sustained:
        best = max(sustained, key=lambda r: r.achieved_produce_rate)
        print(f"  Highest sustained rate : {best.achieved_produce_rate:,.0f} events/sec")
        print(f"    at that rate, p50    : {best.p50_ms:,.0f} ms")
        print(f"    at that rate, p95    : {best.p95_ms:,.0f} ms")
        print(f"    at that rate, p99    : {best.p99_ms:,.0f} ms")
        print(f"    delivery             : {best.delivery_pct:.1f}%")
    else:
        print("  No step sustained >=95% delivery without being generator-bound.")

    import os

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(
            {
                "generated_at": iso_now(),
                "output_topic": OUTPUT_TOPIC,
                "step_seconds": args.step_seconds,
                "drain_seconds": args.drain_seconds,
                "steps": [asdict(r) for r in results],
            },
            fh,
            indent=2,
        )
    print(f"\n  results written to {args.out}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
