"""
PagedOut's cascade-attribution principle, applied to the RCAEval benchmark.

PagedOut's investigator does not rank services by how loudly they are
failing. It asks a structural question:

    Is this service broken, or is it merely downstream of something broken?

A service that is anomalous *and* depends on another anomalous service is
probably a victim. The root cause is the anomalous service with no anomalous
dependency beneath it. In PagedOut that check runs against live /health
endpoints and a declared topology (agents/tools.py::check_dependencies).

RCAEval gives neither: it gives time-series metrics and nothing else. So the
principle transfers but the implementation cannot. Here:

  * "anomalous" is a change statistic across the injection boundary
    rather than a health endpoint
  * the topology is Online Boutique's published call graph rather than
    PagedOut's declared DEPENDENCIES

Two rankers are provided so the contribution of the structural step can be
isolated:

  rank_flat     pure anomaly score. The obvious baseline: blame whatever
                looks worst.
  rank_cascade  the same scores, demoted when a dependency is also lit up.

If cascade attribution is worth anything, the second beats the first. If it
does not, that is a result too, and it gets reported.

No parameter here was tuned against the benchmark. DEMOTION was fixed at 1.0
before the first evaluation run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Online Boutique call graph ────────────────────────────────────────────────
# From the published architecture of GoogleCloudPlatform/microservices-demo.
# Edges point from caller to callee, so a service's "downstream" is what it
# depends on — the direction a cascade travels *up* from.
TOPOLOGY: dict[str, list[str]] = {
    "frontend": [
        "adservice", "cartservice", "checkoutservice", "currencyservice",
        "productcatalogservice", "recommendationservice", "shippingservice",
    ],
    "checkoutservice": [
        "cartservice", "currencyservice", "emailservice", "paymentservice",
        "productcatalogservice", "shippingservice",
    ],
    "cartservice": ["redis"],
    "recommendationservice": ["productcatalogservice"],
    # Leaves — nothing beneath them, so they can never be cascade victims.
    "adservice": [],
    "currencyservice": [],
    "emailservice": [],
    "paymentservice": [],
    "productcatalogservice": [],
    "shippingservice": [],
    "redis": [],
}

# ── Sock Shop call graph ──────────────────────────────────────────────────────
# From the published architecture of microservices-demo (Weaveworks). Deeper
# than Online Boutique: every business service sits in front of its own
# database, so there are two-hop cascades (front-end -> carts -> carts-db)
# that Online Boutique mostly lacks.
TOPOLOGY_SOCKSHOP: dict[str, list[str]] = {
    "front-end": ["catalogue", "carts", "orders", "user"],
    "orders": ["carts", "payment", "shipping", "user"],
    "carts": ["carts-db"],
    "catalogue": ["catalogue-db"],
    "user": ["user-db"],
    "shipping": ["rabbitmq"],
    "rabbitmq": [],
    "queue-master": ["rabbitmq"],
    "payment": [],
    "carts-db": [],
    "catalogue-db": [],
    "orders-db": [],
    "user-db": [],
    "session-db": [],
    "rabbitmq-exporter": [],
}

TOPOLOGIES = {
    "RE1-OB": TOPOLOGY,
    "RE1-SS": TOPOLOGY_SOCKSHOP,
}

# How hard to demote a service whose dependency is also anomalous. 1.0 means
# a victim whose dependency is equally anomalous is halved. Chosen a priori.
DEMOTION = 1.0

# `main` is the host/node aggregate, not a service. Excluded from ranking:
# it is never the annotated root cause and would otherwise absorb every
# node-level CPU signal.
NON_SERVICES = {"main", "time"}


def _split(df: pd.DataFrame, inject_time: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    return df[df.time < inject_time], df[df.time >= inject_time]


def anomaly_scores(df: pd.DataFrame, inject_time: int) -> dict[str, float]:
    """Per-service anomaly score across the injection boundary.

    For each metric column, the shift in level after injection is measured in
    units of the metric's own pre-injection variability:

        |median_after - median_before| / (MAD_before + eps)

    Median and MAD rather than mean and standard deviation because these are
    latency and error series: a couple of extreme samples are normal, and a
    mean-based statistic would report them as a regime change.

    A service's score is the MAX over its metrics. Services expose different
    numbers of metrics (frontend has 5, redis has 2); averaging would
    systematically penalise the ones being measured more thoroughly.
    """
    before, after = _split(df, inject_time)
    if len(before) < 10 or len(after) < 10:
        return {}

    scores: dict[str, float] = {}
    for col in df.columns:
        if col in NON_SERVICES:
            continue
        service = col.rsplit("_", 1)[0]
        if service in NON_SERVICES:
            continue

        b = before[col].to_numpy(dtype=float)
        a = after[col].to_numpy(dtype=float)
        b = b[np.isfinite(b)]
        a = a[np.isfinite(a)]
        if len(b) < 10 or len(a) < 10:
            continue

        med_b = np.median(b)
        mad_b = np.median(np.abs(b - med_b))
        # A flat pre-injection series has MAD 0; fall back to a scale derived
        # from the level itself so a constant-then-spiking metric is not
        # scored as infinite.
        scale = mad_b if mad_b > 1e-9 else max(abs(med_b) * 0.01, 1e-6)
        shift = abs(np.median(a) - med_b) / scale

        scores[service] = max(scores.get(service, 0.0), float(shift))

    return scores


def rank_flat(scores: dict[str, float]) -> list[str]:
    """Baseline: blame whatever changed most. No structural reasoning."""
    return [s for s, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def rank_cascade(
    scores: dict[str, float],
    topology: dict[str, list[str]] | None = None,
    demotion: float = DEMOTION,
) -> list[str]:
    """PagedOut's principle: demote services whose dependencies are also lit up.

        adjusted(S) = score(S) / (1 + demotion * max_downstream_ratio(S))

    where max_downstream_ratio is the strongest dependency's score relative to
    S's own. A service whose dependency is far more anomalous than itself is
    almost certainly a victim and falls sharply. A leaf, or a service whose
    dependencies are quiet, is unchanged.

    The ratio rather than the raw downstream score keeps the adjustment scale
    free: what matters is whether the dependency looks *worse than you*, not
    how large the absolute numbers happen to be.
    """
    topo = topology or TOPOLOGY
    if not scores:
        return []

    adjusted: dict[str, float] = {}
    for service, score in scores.items():
        deps = topo.get(service, [])
        downstream = [scores.get(d, 0.0) for d in deps if d in scores]
        if downstream and score > 1e-9:
            ratio = max(downstream) / score
        else:
            ratio = 0.0
        adjusted[service] = score / (1.0 + demotion * ratio)

    return [s for s, _ in sorted(adjusted.items(), key=lambda kv: -kv[1])]
