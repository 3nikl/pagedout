"""
The remediation executor — the system under test.

Per action, the lifecycle is:

    reserve  ->  dry-run  ->  snapshot health  ->  execute
                                                      |
                                              verify the effect
                                                      |
                                        +-------------+-------------+
                                        |                           |
                                   verified                    regressed
                                        |                           |
                                    complete                   roll back

Every step is deliberate:

  reserve   at-most-once, before anything leaves the process
  dry-run   predict the outcome; skip execution entirely when the prediction
            says nothing would change. A no-op that is never sent cannot be
            double-sent.
  snapshot  you cannot verify an effect without a before
  verify    "we sent a request" is not "we fixed the problem"
  rollback  a fix that makes things worse must undo itself

The executor imports no `time`, no `random`, no HTTP client. Everything comes
through Ports, which is what makes it simulable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from idempotency import (
    AlreadyExecuted,
    Ambiguous,
    ExecState,
    IdempotencyLedger,
    derive_key,
)
from ports import Ports, Response
from registry import ActionName, ActionSpec, Risk, lookup

MAX_ATTEMPTS = 3
BASE_BACKOFF = 0.5
DEADLINE = 30.0


class Outcome(str, Enum):
    EXECUTED = "executed"            # ran and verified
    SKIPPED_NO_OP = "skipped_no_op"  # dry-run predicted no change
    SKIPPED_DUPLICATE = "skipped_duplicate"  # already completed under this key
    AWAITING_APPROVAL = "awaiting_approval"
    ROLLED_BACK = "rolled_back"      # ran, regressed, reverted
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"          # outcome genuinely unknown
    REJECTED = "rejected"            # not on the allowlist / invalid params


@dataclass
class ActionResult:
    action: str
    target: str
    outcome: Outcome
    key: str = ""
    attempts: int = 0
    detail: str = ""
    changed: bool = False
    health_before: dict[str, Any] = field(default_factory=dict)
    health_after: dict[str, Any] = field(default_factory=dict)

    @property
    def executed_side_effect(self) -> bool:
        """Did this action actually change the world?

        Used by the invariant checker. ROLLED_BACK counts: the side effect
        did happen, it was merely undone afterwards.
        """
        return self.outcome in (Outcome.EXECUTED, Outcome.ROLLED_BACK)


@dataclass
class ExecutionReport:
    incident_id: str
    results: list[ActionResult] = field(default_factory=list)
    approvals_required: list[str] = field(default_factory=list)

    @property
    def side_effects(self) -> list[ActionResult]:
        return [r for r in self.results if r.executed_side_effect]


class RemediationExecutor:
    def __init__(
        self,
        ports: Ports,
        max_attempts: int = MAX_ATTEMPTS,
        base_backoff: float = BASE_BACKOFF,
        deadline: float = DEADLINE,
        approvals: set[str] | None = None,
    ):
        self.ports = ports
        self.ledger = IdempotencyLedger(ports.store, ports.clock)
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.deadline = deadline
        # Granted approvals, keyed by idempotency key. A high-risk action
        # executes only if its key appears here.
        self.approvals = approvals or set()

    # ── health ────────────────────────────────────────────────────────────────

    def _health(self, service: str) -> dict[str, Any]:
        resp = self.ports.transport.get(service, "/health", timeout=3.0)
        return resp.body if resp.ok else {}

    # ── dry run ───────────────────────────────────────────────────────────────

    def _dry_run(self, spec: ActionSpec, health: dict[str, Any],
                 params: dict[str, Any]) -> tuple[bool, str]:
        """Predict whether executing would change anything.

        Returns (would_change, reason). A prediction of "no change" skips
        execution entirely, which is the cheapest possible form of safety:
        the request is never sent, so it cannot be sent twice.
        """
        if not health:
            # No health data means no basis for a prediction. Proceed rather
            # than skip — refusing to act because monitoring is down is its
            # own kind of outage.
            return True, "no health data; proceeding without prediction"

        if spec.name is ActionName.DRAIN_POOL:
            in_use = float(health.get("pool_in_use", 0))
            if in_use == 0:
                return False, "pool already empty"
            return True, f"would release {in_use:.0f} connections"

        if spec.name is ActionName.RESIZE_POOL:
            if health.get("pool_size") == params.get("size"):
                return False, f"pool already sized {params.get('size')}"
            return True, f"would resize {health.get('pool_size')} -> {params.get('size')}"

        if spec.name is ActionName.CLEAR_CACHE:
            entries = int(health.get("cache_entries", 0) or 0)
            if entries == 0:
                return False, "cache already empty"
            return True, f"would clear {entries} entries"

        if spec.name is ActionName.ROLLBACK_VERSION:
            return True, f"would roll back from {health.get('version')}"

        return True, "no predictor for this action"

    # ── single action ─────────────────────────────────────────────────────────

    def execute_action(
        self,
        incident_id: str,
        action: str,
        target: str,
        params: dict[str, Any] | None = None,
    ) -> ActionResult:
        params = params or {}

        # 1. Allowlist and parameter validation. Anything the registry does
        #    not know cannot run, whatever the planner said.
        try:
            spec = lookup(action)
            spec.validate_params(params)
        except ValueError as exc:
            return ActionResult(action, target, Outcome.REJECTED, detail=str(exc))

        if spec.name is ActionName.ESCALATE:
            return ActionResult(action, target, Outcome.AWAITING_APPROVAL,
                                detail="escalated to a human")

        key = derive_key(incident_id, action, target, params)

        # 2. Approval gate, checked BEFORE reserving. A blocked action should
        #    leave no trace that could later look retryable.
        if spec.risk is Risk.HIGH and key not in self.approvals:
            return ActionResult(action, target, Outcome.AWAITING_APPROVAL, key=key,
                                detail=f"{spec.risk.value}-risk action requires approval")

        # 3. Reserve. At-most-once starts here.
        try:
            res = self.ledger.reserve(key, incident_id, action, target)
        except AlreadyExecuted as exc:
            return ActionResult(action, target, Outcome.SKIPPED_DUPLICATE, key=key,
                                attempts=exc.reservation.attempts,
                                detail="already completed under this key")
        except Ambiguous as exc:
            resolved = self._reconcile(spec, target, exc.reservation)
            if resolved is not None:
                return resolved
            return ActionResult(action, target, Outcome.AMBIGUOUS, key=key,
                                detail="previous attempt outcome unknown")

        # 4. Snapshot, then predict.
        before = self._health(target)
        would_change, reason = self._dry_run(spec, before, params)
        if not would_change:
            self.ledger.complete(res, {"skipped": True, "reason": reason})
            return ActionResult(action, target, Outcome.SKIPPED_NO_OP, key=key,
                                detail=reason, health_before=before)

        # 5. Execute with bounded retry.
        started = self.ports.clock.now()
        last_error = ""
        response: Response | None = None

        while res.attempts < self.max_attempts:
            if self.ports.clock.now() - started > self.deadline:
                last_error = "deadline exceeded"
                break

            res = self.ledger.mark_in_flight(res)
            response = self.ports.transport.post(
                target, spec.path, params, idempotency_key=key, timeout=5.0
            )

            if response.ok:
                break

            if response.ambiguous:
                # The request may have landed. Do NOT blindly retry — look at
                # the world and decide.
                #
                # GROUNDHOG FINDING (seed 3, at_most_once): the first version
                # of this branch probed health once and, if the probe came
                # back empty, fell through to retry. When the network dropped
                # the ACK *and* the probe, the action executed twice. The
                # defence had a failure mode of its own.
                #
                # Now the probe is retried, and an unobservable state fails
                # CLOSED: we stop and escalate rather than risk a duplicate.
                # "Cannot observe" must never be treated as "did not happen".
                observed, after = self._probe_effect(spec, target, before)

                if observed is True:
                    self.ledger.complete(res, {"reconciled": True})
                    return ActionResult(action, target, Outcome.EXECUTED, key=key,
                                        attempts=res.attempts, changed=True,
                                        detail="ack lost, effect confirmed by probe",
                                        health_before=before, health_after=after)

                if observed is None:
                    self.ledger.fail(
                        res, "ambiguous: effect unobservable, not retried"
                    )
                    return ActionResult(action, target, Outcome.AMBIGUOUS, key=key,
                                        attempts=res.attempts,
                                        detail="ack lost and effect unobservable; "
                                               "failing closed to avoid duplicate",
                                        health_before=before)

                # observed is False: the effect definitively did NOT happen,
                # so retrying is safe.
                last_error = response.error or "ambiguous"
            else:
                last_error = response.error or f"status {response.status}"

            self.ports.clock.sleep(
                self.ports.rng.jitter(self.base_backoff * (2 ** (res.attempts - 1)))
            )

        if response is None or not response.ok:
            self.ledger.fail(res, last_error)
            return ActionResult(action, target, Outcome.FAILED, key=key,
                                attempts=res.attempts, detail=last_error,
                                health_before=before)

        # 6. Verify the effect, and roll back if it made things worse.
        after = self._health(target)
        changed = bool(response.body.get("changed", True))

        if spec.verify and before and after and not spec.verify(before, after):
            reverted = self._rollback(spec, target, before, params)
            self.ledger.complete(res, {"rolled_back": True, "reverted": reverted})
            return ActionResult(action, target, Outcome.ROLLED_BACK, key=key,
                                attempts=res.attempts, changed=changed,
                                detail=f"verification failed; reverted={reverted}",
                                health_before=before, health_after=after)

        self.ledger.complete(res, {"changed": changed, "body": response.body})
        return ActionResult(action, target, Outcome.EXECUTED, key=key,
                            attempts=res.attempts, changed=changed,
                            detail="verified", health_before=before,
                            health_after=after)

    # ── recovery and rollback ─────────────────────────────────────────────────

    def _probe_effect(
        self, spec: ActionSpec, target: str, before: dict[str, Any],
        attempts: int = 3,
    ) -> tuple[bool | None, dict[str, Any]]:
        """Did the action take effect? Returns (True | False | None, health).

        None means "could not determine" — the probe itself failed, or the
        action has no verify predicate. That is deliberately distinct from
        False. Collapsing the two is what caused the seed-3 double execution:
        an unanswerable question was treated as a "no".

        The probe is retried with backoff, because a single dropped GET
        should not be enough to make the system give up on knowing.
        """
        if spec.verify is None or not before:
            return None, {}

        for attempt in range(attempts):
            after = self._health(target)
            if after:
                return bool(spec.verify(before, after)), after
            if attempt < attempts - 1:
                self.ports.clock.sleep(
                    self.ports.rng.jitter(self.base_backoff * (2 ** attempt))
                )
        return None, {}

    def _reconcile(
        self, spec: ActionSpec, target: str, res
    ) -> ActionResult | None:
        """Settle an IN_FLIGHT reservation by observing the service.

        Called when reserve() finds a previous attempt whose outcome is
        unknown — typically after a crash. Returns a result if it can be
        settled, None if it genuinely cannot.
        """
        if spec.verify is None:
            return None
        after = self._health(target)
        if not after:
            return None

        # Without a stored "before" we can only check an absolute condition.
        # Treat a currently-healthy service as evidence the action landed.
        if after.get("healthy") is True:
            self.ledger.resolve_ambiguous(res, observed_effect=True)
            return ActionResult(res.action, target, Outcome.SKIPPED_DUPLICATE,
                                key=res.key, attempts=res.attempts,
                                detail="reconciled after crash: effect observed",
                                health_after=after)
        self.ledger.resolve_ambiguous(res, observed_effect=False)
        return None

    def _rollback(
        self, spec: ActionSpec, target: str, before: dict[str, Any],
        params: dict[str, Any]
    ) -> bool:
        """Undo an action that failed verification."""
        if spec.inverse is None:
            return False
        inverse = lookup(spec.inverse.value)

        inverse_params: dict[str, Any] = {}
        if inverse.name is ActionName.RESIZE_POOL:
            prior = before.get("pool_size")
            if not isinstance(prior, int):
                return False
            inverse_params = {"size": prior}

        resp = self.ports.transport.post(
            target, inverse.path, inverse_params,
            idempotency_key=f"rollback:{derive_key(target, inverse.name.value, target, inverse_params)}",
            timeout=5.0,
        )
        return resp.ok

    # ── plan ──────────────────────────────────────────────────────────────────

    def execute_plan(self, incident_id: str, plan: dict[str, Any]) -> ExecutionReport:
        """Run a validated plan, stopping at the first hard failure.

        Sequential and fail-fast on purpose. Actions in a plan are ordered
        least disruptive first; continuing past a failure would apply a more
        disruptive action on top of a system already in an unexpected state.
        """
        report = ExecutionReport(incident_id=incident_id)

        for step in plan.get("actions", []):
            result = self.execute_action(
                incident_id,
                step.get("action", ""),
                step.get("target_service", ""),
                step.get("parameters", {}),
            )
            report.results.append(result)

            if result.outcome is Outcome.AWAITING_APPROVAL and result.key:
                report.approvals_required.append(result.key)

            if result.outcome in (Outcome.FAILED, Outcome.AMBIGUOUS):
                break

        return report
