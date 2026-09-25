"""
Retrieval benchmark: latency and accuracy, baseline versus optimised.

Compares three configurations over the same corpus and the same queries:

  exact_dense   BASELINE. Brute-force scan, HNSW disabled (m=0), dense only.
  hnsw_dense    HNSW graph (m=32, ef_construct=200), dense only.
  hybrid_rrf    HNSW + BM25 sparse, fused with Reciprocal Rank Fusion.

Reports p50/p95/p99 latency and Recall@3 / MRR for each. The point is that
"we made retrieval faster and better" should be two measured numbers with a
baseline, not an assertion.

Query set and its honest limitations:

  symptom queries (73)   short phrases taken from each runbook's `symptoms`
                         list, expected answer = that runbook.
  log-line queries (20)  hand-written realistic log lines and alert titles,
                         expected answer = any document of that incident
                         type. These are NOT drawn from the corpus.

Caveat worth stating plainly: symptom strings are part of the text that gets
embedded, so the symptom family is closer to a retrieval sanity check than a
generalisation test, and its recall is an upper bound. The log-line family is
the honest signal. Both are reported, and either way the comparison BETWEEN
the three configurations is valid, because all three see identical queries.

Usage:
    python rag/benchmark.py
    python rag/benchmark.py --repeats 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from corpus import RUNBOOKS as RUNBOOKS_JSON
from retrieve import HybridRetriever

OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmarks" / "retrieval_results.json"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_query_set() -> list[tuple[str, str, str]]:
    """(query, expected_doc_id, expected_incident_type).

    Two families:
      - symptom phrases from each runbook
      - realistic log lines, which is what the agent actually sends
    """
    queries: list[tuple[str, str, str]] = []

    # Symptom phrases, read from the runbook source rather than the title.
    # Using the title as the query would be circular: it is the first thing
    # in the indexed text, so every retriever would score ~100% and the
    # benchmark would measure nothing.
    runbooks = json.loads(RUNBOOKS_JSON.read_text())
    for i, rb in enumerate(runbooks):
        doc_id = f"rb_{i:03d}"
        for symptom in rb.get("symptoms", []):
            queries.append((symptom, doc_id, rb["incident_type"]))

    # Hand-written operational queries. These are the realistic case: a log
    # line or an alert title, not a tidy question.
    realistic = [
        ("FATAL: Connection pool exhausted. Active connections: 20/20",
         "database_connection_exhaustion"),
        ("Unable to acquire database connection after 30s timeout",
         "database_connection_exhaustion"),
        ("HikariPool-1 - Connection is not available, request timed out",
         "database_connection_exhaustion"),
        ("ERROR: GC overhead limit exceeded", "memory_leak"),
        ("java.lang.OutOfMemoryError: Java heap space", "memory_leak"),
        ("Heap usage at 94% approaching OOM threshold", "memory_leak"),
        ("Container terminated with exit code 137 OOMKilled", "pod_crash_loop"),
        ("Back-off restarting failed container CrashLoopBackOff", "pod_crash_loop"),
        ("Liveness probe failed: HTTP 500", "pod_crash_loop"),
        ("Request latency p99 at 4200ms, SLA is 500ms", "high_latency_spike"),
        ("Upstream timeout after 3000ms", "high_latency_spike"),
        ("No space left on device", "disk_space_critical"),
        ("Log rotation failed, disk at 97%", "disk_space_critical"),
        ("Failed to connect to peer: Connection refused", "network_partition"),
        ("gRPC stream disconnected, reconnecting", "network_partition"),
        ("CPU throttling detected, throttled time 78%", "cpu_throttling"),
        ("Thread pool queue depth 180, consider scaling", "cpu_throttling"),
        ("NullPointerException in OrderValidator after deploy", "deployment_failure"),
        ("Readiness probe failing for new ReplicaSet", "deployment_failure"),
        ("Error rate jumped 85% following release", "deployment_failure"),
    ]
    for text, itype in realistic:
        queries.append((text, "", itype))

    return queries


def evaluate(
    retriever: HybridRetriever,
    queries: list[tuple[str, str, str]],
    mode: str,
    ef: int,
    repeats: int,
) -> dict:
    latencies: list[float] = []
    embed_times: list[float] = []
    search_times: list[float] = []
    hits_at_3 = 0
    reciprocal_ranks: list[float] = []
    scored = 0

    # Warm up: first call pays for lazy model graph setup and an empty
    # Qdrant page cache. Including it would put a one-off cost into p99.
    for q, _, _ in queries[:5]:
        retriever.search(q, top_k=3, mode=mode, ef=ef)

    for _ in range(repeats):
        for query, expected_doc, expected_type in queries:
            hits, embed_ms, search_ms = retriever.search_breakdown(
                query, top_k=3, mode=mode, ef=ef
            )
            latencies.append(embed_ms + search_ms)
            embed_times.append(embed_ms)
            search_times.append(search_ms)

            if not expected_type:
                continue
            scored += 1

            # A hit counts as correct if it is the exact expected runbook, or
            # (for the realistic log-line queries, which have no single right
            # answer) any document of the expected incident type.
            rank = None
            for i, h in enumerate(hits, start=1):
                ok = (
                    (expected_doc and h.doc_id == expected_doc)
                    or (not expected_doc and h.incident_type == expected_type)
                )
                if ok:
                    rank = i
                    break

            if rank:
                hits_at_3 += 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

    return {
        "mode": mode,
        "ef": ef,
        "queries": len(queries),
        "repeats": repeats,
        "samples": len(latencies),
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "mean_ms": round(statistics.mean(latencies), 2),
        "embed_p50_ms": round(percentile(embed_times, 50), 2),
        "search_p50_ms": round(percentile(search_times, 50), 2),
        "search_p95_ms": round(percentile(search_times, 95), 2),
        "search_p99_ms": round(percentile(search_times, 99), 2),
        "recall_at_3": round(hits_at_3 / scored, 4) if scored else 0.0,
        "mrr": round(statistics.mean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--ef", type=int, default=128)
    args = ap.parse_args()

    queries = build_query_set()
    print(f"Query set: {len(queries)} queries x {args.repeats} repeats\n")

    configs = [
        ("exact_dense  (BASELINE, no HNSW)", "runbooks_exact", "dense"),
        ("hnsw_dense   (HNSW m=32)", "runbooks", "dense"),
        ("hybrid_rrf   (HNSW + BM25 RRF)", "runbooks", "hybrid"),
    ]

    print(f"{'configuration':34} {'total p95':>10} {'embed p50':>10} "
          f"{'search p50':>11} {'search p95':>11} {'search p99':>11} "
          f"{'recall@3':>9} {'MRR':>7}")
    print("-" * 118)

    results = []
    for label, collection, mode in configs:
        r = HybridRetriever(collection=collection)
        stats = evaluate(r, queries, mode, args.ef, args.repeats)
        stats["label"] = label.strip()
        stats["collection"] = collection
        results.append(stats)
        print(f"{label:34} {stats['p95_ms']:>9.1f}ms {stats['embed_p50_ms']:>9.2f}ms "
              f"{stats['search_p50_ms']:>10.2f}ms {stats['search_p95_ms']:>10.2f}ms "
              f"{stats['search_p99_ms']:>10.2f}ms {stats['recall_at_3']:>8.1%} "
              f"{stats['mrr']:>7.3f}")

    print("-" * 118)

    base, hybrid = results[0], results[-1]
    print(f"\n  BASELINE (exact scan) -> HYBRID (HNSW + BM25 RRF)")
    for field, name in (("search_p50_ms", "search p50"),
                        ("search_p95_ms", "search p95"),
                        ("search_p99_ms", "search p99")):
        b, h = base[field], hybrid[field]
        factor = (b / h) if h else 0.0
        print(f"  {name:12}: {b:7.2f}ms -> {h:6.2f}ms  "
              f"({factor:.2f}x {'faster' if factor > 1 else 'slower'})")
    print(f"  embedding is a fixed ~{hybrid['embed_p50_ms']:.1f}ms per query "
          f"and dominates total latency at this corpus size")
    print(f"  recall@3    : {base['recall_at_3']:.1%} -> {hybrid['recall_at_3']:.1%}")
    print(f"  MRR         : {base['mrr']:.3f} -> {hybrid['mrr']:.3f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_documents": 1342,
        "configs": results,
    }, indent=2))
    print(f"\n  written to {OUT}")


if __name__ == "__main__":
    main()
