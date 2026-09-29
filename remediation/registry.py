"""
Action registry — the allowlist of what remediation is permitted to do.

Two properties make this a security boundary rather than a lookup table:

  1. It is CLOSED. An action not declared here cannot execute, no matter what
     the planner emits. The LLM proposes; the registry disposes.

  2. Risk is declared HERE, by us, not inferred from the action or asserted by
     the model. The previous implementation classified risk with
     `step.startswith("SAFE:")`, which made a runbook author's typo into a
     privilege escalation.

Each spec also declares how to VERIFY the action worked and how to INVERT it.
Without a verify predicate you cannot tell remediation from noise; without an
inverse you cannot roll back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Risk(str, Enum):
    LOW = "low"      # auto-executable
    HIGH = "high"    # requires human approval


class ActionName(str, Enum):
    DRAIN_POOL = "drain_pool"
    RESIZE_POOL = "resize_pool"
    CLEAR_CACHE = "clear_cache"
    ROLLBACK_VERSION = "rollback_version"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ParamSpec:
    type: type
    minimum: int | None = None
    maximum: int | None = None

    def validate(self, name: str, value: Any) -> None:
        # bool is a subclass of int in Python, so `isinstance(True, int)` is
        # True. Without this guard, size=True would validate and then mean
        # "resize the pool to 1 connection".
        if self.type is int and isinstance(value, bool):
            raise ValueError(f"{name}: expected int, got bool")
        if not isinstance(value, self.type):
            raise ValueError(
                f"{name}: expected {self.type.__name__}, got {type(value).__name__}"
            )
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{name}: {value} below minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{name}: {value} above maximum {self.maximum}")


@dataclass(frozen=True)
class ActionSpec:
    name: ActionName
    description: str
    method: str
    path: str
    risk: Risk
    params: dict[str, ParamSpec] = field(default_factory=dict)
    fixes: tuple[str, ...] = ()

    # Given the service health payload before and after, did this action do
    # what it claimed? Returning False triggers rollback.
    verify: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None

    # The action that undoes this one, if any. None means irreversible, which
    # is itself important information: irreversible actions must never be
    # auto-executed.
    inverse: ActionName | None = None

    @property
    def reversible(self) -> bool:
        return self.inverse is not None or self.risk is Risk.LOW

    def validate_params(self, given: dict[str, Any]) -> None:
        unexpected = set(given) - set(self.params)
        if unexpected:
            raise ValueError(
                f"{self.name.value}: unexpected parameters {sorted(unexpected)}"
            )
        for pname, spec in self.params.items():
            if pname not in given:
                raise ValueError(f"{self.name.value}: missing parameter '{pname}'")
            spec.validate(pname, given[pname])


# ── Verification predicates ───────────────────────────────────────────────────
# Each answers: comparing health before and after, did the intended thing
# actually happen? These are the difference between "we sent a request" and
# "we fixed the problem".


def _pool_drained(before: dict, after: dict) -> bool:
    return float(after.get("pool_utilization", 1.0)) < float(
        before.get("pool_utilization", 0.0)
    ) or float(after.get("pool_utilization", 1.0)) == 0.0


def _pool_resized(before: dict, after: dict) -> bool:
    return after.get("pool_size") != before.get("pool_size")


def _heap_released(before: dict, after: dict) -> bool:
    return float(after.get("heap_pct", 100.0)) <= float(before.get("heap_pct", 0.0))


def _rolled_back_to_target(before: dict, after: dict) -> bool:
    """Did the service land on the specific version we were rolling back TO?

    GROUNDHOG FINDING (seed 149, at_most_once): this predicate used to be
    `after.version != before.version` — a difference check. But
    rollback_version is an INVOLUTION: applying it twice returns to the
    starting version. When the network duplicated the request, the service
    ended up back on the bad version, the difference check saw "nothing
    changed", concluded the action had not happened, and retried it.

    A difference check can never verify a self-inverse operation, because an
    even number of applications is indistinguishable from zero. Asserting the
    TARGET STATE instead is immune to that: landing on the expected version
    is true after one application and false after two.
    """
    target = before.get("previous_version")
    if not target:
        # Without a target we cannot verify. Returning False here would mean
        # "did not happen", which is the exact conflation that caused the
        # seed-3 bug. The executor treats a missing predicate as unknown.
        return False
    return after.get("version") == target


REGISTRY: dict[ActionName, ActionSpec] = {
    ActionName.DRAIN_POOL: ActionSpec(
        name=ActionName.DRAIN_POOL,
        description="Release idle database connections and clear pool saturation.",
        method="POST",
        path="/admin/pool/drain",
        risk=Risk.LOW,
        fixes=("database_connection_exhaustion",),
        verify=_pool_drained,
    ),
    ActionName.RESIZE_POOL: ActionSpec(
        name=ActionName.RESIZE_POOL,
        description="Change connection pool capacity.",
        method="POST",
        path="/admin/pool/resize",
        risk=Risk.HIGH,
        params={"size": ParamSpec(int, minimum=1, maximum=500)},
        fixes=("database_connection_exhaustion",),
        verify=_pool_resized,
        inverse=ActionName.RESIZE_POOL,
    ),
    ActionName.CLEAR_CACHE: ActionSpec(
        name=ActionName.CLEAR_CACHE,
        description="Drop the local cache and release heap held by it.",
        method="POST",
        path="/admin/cache/clear",
        risk=Risk.LOW,
        fixes=("memory_leak",),
        verify=_heap_released,
    ),
    ActionName.ROLLBACK_VERSION: ActionSpec(
        name=ActionName.ROLLBACK_VERSION,
        description="Roll the service back to its previous deployed version.",
        method="POST",
        path="/admin/version/rollback",
        risk=Risk.HIGH,
        fixes=("deployment_failure", "pod_crash_loop"),
        verify=_rolled_back_to_target,
        inverse=ActionName.ROLLBACK_VERSION,
    ),
    ActionName.ESCALATE: ActionSpec(
        name=ActionName.ESCALATE,
        description="Hand off to a human engineer.",
        method="",
        path="",
        risk=Risk.LOW,
        fixes=(),
    ),
}


def lookup(name: str) -> ActionSpec:
    """Resolve an action name, rejecting anything not on the allowlist."""
    try:
        return REGISTRY[ActionName(name)]
    except (ValueError, KeyError):
        raise ValueError(
            f"action {name!r} is not in the registry; "
            f"allowed: {sorted(a.value for a in ActionName)}"
        ) from None


def actions_for(incident_type: str, risk: Risk | None = None) -> list[ActionSpec]:
    """Registry actions that claim to fix this incident type."""
    return [
        spec
        for spec in REGISTRY.values()
        if incident_type in spec.fixes and (risk is None or spec.risk is risk)
    ]
