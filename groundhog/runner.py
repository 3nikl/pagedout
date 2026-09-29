"""
Groundhog runner: run(seed) -> Trace, plus the seed sweep.

One scenario is:

    a service is broken in a known way
    a remediation plan targets it
    the network misbehaves according to the seeded fault profile
    the process may crash and recover at a seeded point

and the question is whether the safety invariants survive.

Determinism is asserted, not assumed: `verify_determinism` re-runs a seed and
compares trace digests. That check is the foundation the whole project rests
on, so it is a first-class function rather than a test.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remediation"))

from executor import RemediationExecutor  # noqa: E402
from idempotency import derive_key  # noqa: E402
from invariants import CheckContext, Violation, check_all  # noqa: E402
from ports import Ports  # noqa: E402
from sim import Crashed, EventKind, SeededRng, Trace, VirtualClock  # noqa: E402
from world import FaultProfile, SimRng, SimService, SimStore, SimTransport  # noqa: E402


# ── Scenarios ─────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    """A named starting condition plus the plan the agent produced for it."""

    name: str
    incident_type: str
    target: str
    broken: dict[str, Any]
    plan: dict[str, Any]
    approve_high_risk: bool = False


def _services() -> dict[str, SimService]:
    return {
        "checkout-service": SimService("checkout-service"),
        "payment-service": SimService("payment-service"),
        "ledger-service": SimService("ledger-service"),
    }


SCENARIOS: list[Scenario] = [
    Scenario(
        name="pool_exhaustion_drain",
        incident_type="database_connection_exhaustion",
        target="ledger-service",
        broken={"pool_in_use": 20, "faults": {"pool_exhaustion"}},
        plan={"actions": [
            {"action": "drain_pool", "target_service": "ledger-service",
             "parameters": {}, "rationale": "release saturated connections"},
        ]},
    ),
    Scenario(
        name="memory_leak_clear_cache",
        incident_type="memory_leak",
        target="payment-service",
        broken={"heap_pct": 94.0, "cache_entries": 5000, "faults": {"memory_leak"}},
        plan={"actions": [
            {"action": "clear_cache", "target_service": "payment-service",
             "parameters": {}, "rationale": "release heap held by cache"},
        ]},
    ),
    Scenario(
        name="bad_deploy_rollback",
        incident_type="deployment_failure",
        target="checkout-service",
        broken={"version": "v2.3.1", "faults": {"bad_deploy"}},
        plan={"actions": [
            {"action": "rollback_version", "target_service": "checkout-service",
             "parameters": {}, "rationale": "revert the regressing release"},
        ]},
        approve_high_risk=True,
    ),
    Scenario(
        name="pool_multi_step",
        incident_type="database_connection_exhaustion",
        target="ledger-service",
        broken={"pool_in_use": 20, "faults": {"pool_exhaustion"}},
        plan={"actions": [
            {"action": "drain_pool", "target_service": "ledger-service",
             "parameters": {}, "rationale": "release connections first"},
            {"action": "resize_pool", "target_service": "ledger-service",
             "parameters": {"size": 60}, "rationale": "raise capacity"},
        ]},
        approve_high_risk=True,
    ),
    Scenario(
        name="unapproved_high_risk",
        incident_type="deployment_failure",
        target="checkout-service",
        broken={"version": "v2.3.1", "faults": {"bad_deploy"}},
        plan={"actions": [
            {"action": "rollback_version", "target_service": "checkout-service",
             "parameters": {}, "rationale": "revert"},
        ]},
        approve_high_risk=False,   # must NOT execute
    ),
]


# ── Run result ────────────────────────────────────────────────────────────────


@dataclass
class RunResult:
    seed: int
    scenario: str
    trace: Trace
    violations: list[Violation] = field(default_factory=list)
    crashed: bool = False
    recovered: bool = False
    side_effect_count: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.violations)


# ── The run ───────────────────────────────────────────────────────────────────


def run(seed: int, scenario: Scenario, profile: FaultProfile | None = None) -> RunResult:
    """Execute one scenario under one seed. Fully deterministic."""
    rng = SeededRng(seed)
    clock = VirtualClock()
    trace = Trace(seed)
    profile = profile or FaultProfile()

    services = _services()
    svc = services[scenario.target]
    for attr, value in scenario.broken.items():
        setattr(svc, attr, set(value) if isinstance(value, set) else value)

    # Decide the crash point from the seed, before anything runs, so it is
    # part of the reproducible scenario rather than a timing accident.
    crash_at = None
    if rng.chance(profile.crash_probability):
        crash_at = rng.randint(1, 12)
        trace.record(clock, EventKind.NOTE, "runner",
                     "crash scheduled", at_op=crash_at)

    store = SimStore(trace, clock)
    incident_id = f"inc-{scenario.name}-{seed}"

    approved: set[str] = set()
    if scenario.approve_high_risk:
        for step in scenario.plan["actions"]:
            approved.add(derive_key(
                incident_id, step["action"], step["target_service"],
                step.get("parameters", {}),
            ))

    def build_executor() -> RemediationExecutor:
        transport = SimTransport(services, rng, clock, trace, profile, crash_at)
        return RemediationExecutor(
            Ports(clock=clock, rng=SimRng(rng), transport=transport, store=store),
            approvals=set(approved),
        )

    crashed = recovered = False
    report = None

    try:
        report = build_executor().execute_plan(incident_id, scenario.plan)
    except Crashed:
        crashed = True
        # Recover the way a restarted process would: discard uncommitted WAL
        # entries, then resume the same plan. This is where replay bugs live.
        store.crash()
        store.recover()
        trace.record(clock, EventKind.RECOVER, "process", "restarted after crash")
        crash_at = None
        try:
            report = build_executor().execute_plan(incident_id, scenario.plan)
            recovered = True
        except Crashed:
            pass

    if report is not None:
        setattr(report, "approved_keys", approved)

    ctx = CheckContext(trace=trace, services=services, report=report,
                       crashed=crashed, recovered=recovered)
    violations = check_all(ctx)

    return RunResult(
        seed=seed,
        scenario=scenario.name,
        trace=trace,
        violations=violations,
        crashed=crashed,
        recovered=recovered,
        side_effect_count=len(trace.side_effects),
    )


# ── Determinism check ─────────────────────────────────────────────────────────


def verify_determinism(scenario: Scenario, seeds: list[int]) -> tuple[bool, str]:
    """Re-run each seed and confirm the trace digests match exactly.

    This is the load-bearing assertion of the entire project. If it fails,
    every finding is unreproducible and Groundhog is worthless.
    """
    for seed in seeds:
        first = run(seed, scenario).trace.digest()
        second = run(seed, scenario).trace.digest()
        if first != second:
            return False, (
                f"seed {seed} on {scenario.name}: {first[:16]} != {second[:16]}"
            )
    return True, f"{len(seeds)} seeds reproduced identically on {scenario.name}"
