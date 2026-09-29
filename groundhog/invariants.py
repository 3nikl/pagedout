"""
Safety invariants — the properties that must hold no matter what the network
and the scheduler do to us.

An invariant here is checked against GROUND TRUTH (what the simulated
services actually did), never against what the executor believes happened.
Checking the executor's own bookkeeping would be circular: a bug in the
bookkeeping is exactly what we are hunting.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from sim import Trace
from world import SimService


@dataclass(frozen=True)
class Violation:
    invariant: str
    detail: str
    evidence: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.invariant}: {self.detail}"


@dataclass
class CheckContext:
    trace: Trace
    services: dict[str, SimService]
    report: Any          # ExecutionReport, or None if the run crashed
    crashed: bool
    recovered: bool


# ── Invariants ────────────────────────────────────────────────────────────────


def at_most_once(ctx: CheckContext) -> list[Violation]:
    """No action may take effect twice for the same idempotency key.

    Ground truth is the SIDE_EFFECT events the simulated services recorded.
    Note the exclusion: a duplicate caused by the NETWORK duplicating a
    packet is a fault of the world, not of the executor — the executor can
    only defend against that if the server dedupes. We count those
    separately so the finding stays honest about whose bug it is.
    """
    by_key: Counter[str] = Counter()
    network_dupes: set[str] = set()

    for effect in ctx.trace.side_effects:
        key = effect.get("key")
        if not key:
            continue
        if effect.get("duplicate"):
            network_dupes.add(key)
            continue
        by_key[key] += 1

    out: list[Violation] = []
    for key, count in by_key.items():
        if count > 1:
            out.append(Violation(
                "at_most_once",
                f"action executed {count} times under one idempotency key",
                {"key": key, "count": count},
            ))
    return out


def no_effect_without_reservation(ctx: CheckContext) -> list[Violation]:
    """Every side effect must carry an idempotency key.

    An unkeyed side effect means something reached a service without going
    through the ledger — so nothing prevents it happening again.
    """
    out: list[Violation] = []
    for effect in ctx.trace.side_effects:
        if not effect.get("key"):
            out.append(Violation(
                "no_effect_without_reservation",
                "side effect executed with no idempotency key",
                {"effect": effect},
            ))
    return out


def high_risk_needs_approval(ctx: CheckContext) -> list[Violation]:
    """A high-risk action must never take effect without prior approval."""
    from registry import Risk, lookup

    approved = getattr(ctx.report, "approved_keys", set()) if ctx.report else set()
    out: list[Violation] = []

    for effect in ctx.trace.side_effects:
        path = effect.get("detail", "")
        action = _action_for_path(path)
        if action is None:
            continue
        try:
            spec = lookup(action)
        except ValueError:
            continue
        if spec.risk is Risk.HIGH and effect.get("key") not in approved:
            # Rollbacks are allowed to invoke a high-risk inverse without a
            # separate approval; they are keyed with a rollback: prefix.
            if str(effect.get("key", "")).startswith("rollback:"):
                continue
            out.append(Violation(
                "high_risk_needs_approval",
                f"{action} took effect without an approval",
                {"effect": effect},
            ))
    return out


def converges_or_escalates(ctx: CheckContext) -> list[Violation]:
    """The run must end in a definite state, never silently stalled.

    A crash that never recovers is a legitimate end state for the simulator;
    an executor that returns normally having neither acted, skipped,
    escalated nor failed is not.
    """
    if ctx.crashed and not ctx.recovered:
        return []
    if ctx.report is None:
        return [Violation("converges_or_escalates",
                          "run produced no execution report", {})]
    if not ctx.report.results:
        return [Violation("converges_or_escalates",
                          "plan produced no results at all", {})]
    return []


def no_action_after_rollback(ctx: CheckContext) -> list[Violation]:
    """Once an incident rolls back, nothing further may act on that service."""
    out: list[Violation] = []
    rolled_back_at: dict[str, int] = {}

    for effect in ctx.trace.side_effects:
        if "rollback" in str(effect.get("detail", "")):
            rolled_back_at.setdefault(effect["actor"], effect["at"])

    for effect in ctx.trace.side_effects:
        service = effect["actor"]
        marker = rolled_back_at.get(service)
        if marker is not None and effect["at"] > marker:
            if "rollback" in str(effect.get("detail", "")):
                continue
            out.append(Violation(
                "no_action_after_rollback",
                f"action on {service} took effect after a rollback",
                {"effect": effect, "rollback_at": marker},
            ))
    return out


_PATH_TO_ACTION = {
    "/admin/pool/drain": "drain_pool",
    "/admin/pool/resize": "resize_pool",
    "/admin/cache/clear": "clear_cache",
    "/admin/version/rollback": "rollback_version",
}


def _action_for_path(path: str) -> str | None:
    return _PATH_TO_ACTION.get(path)


ALL_INVARIANTS: list[Callable[[CheckContext], list[Violation]]] = [
    at_most_once,
    no_effect_without_reservation,
    high_risk_needs_approval,
    converges_or_escalates,
    no_action_after_rollback,
]


def check_all(ctx: CheckContext) -> list[Violation]:
    found: list[Violation] = []
    for invariant in ALL_INVARIANTS:
        try:
            found.extend(invariant(ctx))
        except Exception as exc:  # an invariant must never mask a real bug
            found.append(Violation(
                invariant.__name__,
                f"invariant raised {type(exc).__name__}: {exc}",
                {},
            ))
    return found
