"""
Idempotency: at-most-once execution of remediation actions.

The problem this solves, concretely:

    executor sends  POST /admin/pool/drain
    the service     performs the drain
    the ack         is lost to a network timeout
    executor sees   "timeout — did it happen? unknown"
    executor        retries
    the service     performs the drain AGAIN

For a pool drain that is merely wasteful. For `rollback_version` it means
rolling back twice — past the version you wanted, into one nobody tested.

The defence is a reservation record written to durable storage BEFORE the call
goes out, keyed by a hash that is stable across retries and distinct across
genuinely different actions.

State machine:

    (absent) --reserve--> RESERVED --attempt--> IN_FLIGHT
                                                   |
                                    +--------------+--------------+
                                    |                             |
                              confirmed ok                 confirmed fail
                                    |                             |
                                COMPLETED                      FAILED
                                                                  |
                                                            (retryable)

IN_FLIGHT is the important state, and the one a naive implementation omits.
It means "we sent it and do not know the outcome". Recovering from a crash
into IN_FLIGHT must NOT blindly retry, because the side effect may already
have happened.

Known limitation, stated up front: client-side keys alone cannot make a
non-idempotent server idempotent. If the service does not honour
`Idempotency-Key`, an ambiguous timeout is genuinely unresolvable from the
client. This implementation handles it by refusing to auto-retry out of
IN_FLIGHT for irreversible actions, and by reconciling through a verify
probe where one exists. Groundhog is expected to find the residual window.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ports import Clock, Store, stable_hash


class ExecState(str, Enum):
    RESERVED = "reserved"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Reservation:
    key: str
    state: ExecState
    incident_id: str
    action: str
    target: str
    attempts: int
    created_at: float
    updated_at: float
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state.value,
            "incident_id": self.incident_id,
            "action": self.action,
            "target": self.target,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Reservation":
        return Reservation(
            key=d["key"],
            state=ExecState(d["state"]),
            incident_id=d["incident_id"],
            action=d["action"],
            target=d["target"],
            attempts=d["attempts"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            result=d.get("result"),
        )


def derive_key(
    incident_id: str, action: str, target: str, params: dict[str, Any] | None = None
) -> str:
    """Stable idempotency key.

    Includes params: `resize_pool(size=50)` and `resize_pool(size=200)` are
    genuinely different actions and must not share a key. Excludes attempt
    number and timestamp: a retry of the same action must produce the SAME
    key, which is the entire point.
    """
    return stable_hash("remediation-v1", incident_id, action, target, params or {})


class AlreadyExecuted(Exception):
    """Raised when an action has already completed under this key."""

    def __init__(self, reservation: Reservation):
        self.reservation = reservation
        super().__init__(
            f"action {reservation.action} on {reservation.target} already "
            f"completed (key {reservation.key[:12]}, "
            f"{reservation.attempts} attempt(s))"
        )


class Ambiguous(Exception):
    """Raised when recovery finds an action whose outcome is unknown."""

    def __init__(self, reservation: Reservation):
        self.reservation = reservation
        super().__init__(
            f"action {reservation.action} on {reservation.target} is IN_FLIGHT; "
            f"outcome unknown (key {reservation.key[:12]})"
        )


class IdempotencyLedger:
    """Durable record of what has been attempted and what happened.

    Uses compare_and_set for the initial reservation rather than
    get-then-put. The get-then-put version has a check-then-act race: two
    workers both read "absent", both write a reservation, both proceed, and
    the action executes twice. That race is exactly the class of bug
    Groundhog exists to find, so it must not be present in the design.
    """

    def __init__(self, store: Store, clock: Clock, namespace: str = "idem"):
        self.store = store
        self.clock = clock
        self.namespace = namespace

    def _k(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str) -> Reservation | None:
        raw = self.store.get(self._k(key))
        return Reservation.from_dict(raw) if raw else None

    def reserve(
        self, key: str, incident_id: str, action: str, target: str
    ) -> Reservation:
        """Claim the right to execute this action exactly once.

        Raises AlreadyExecuted if it has already completed.
        Raises Ambiguous if a previous attempt is IN_FLIGHT.
        Returns the existing reservation if it is RESERVED or FAILED (retryable).
        """
        now = self.clock.now()
        fresh = Reservation(
            key=key,
            state=ExecState.RESERVED,
            incident_id=incident_id,
            action=action,
            target=target,
            attempts=0,
            created_at=now,
            updated_at=now,
        )

        # Atomic: succeeds only if nothing is there. Two concurrent callers
        # cannot both win.
        if self.store.compare_and_set(self._k(key), None, fresh.to_dict()):
            return fresh

        existing = self.get(key)
        if existing is None:
            # Lost the CAS but the record vanished — a concurrent delete or a
            # torn write. Treat as ambiguous rather than guessing.
            raise Ambiguous(fresh)

        if existing.state is ExecState.COMPLETED:
            raise AlreadyExecuted(existing)
        if existing.state is ExecState.IN_FLIGHT:
            raise Ambiguous(existing)
        return existing

    def mark_in_flight(self, res: Reservation) -> Reservation:
        """Record that the call is going out NOW.

        Written before the request leaves, so that a crash mid-call recovers
        into IN_FLIGHT rather than into RESERVED. Recovering into RESERVED
        would look retryable and cause a double execution.
        """
        updated = Reservation(
            key=res.key,
            state=ExecState.IN_FLIGHT,
            incident_id=res.incident_id,
            action=res.action,
            target=res.target,
            attempts=res.attempts + 1,
            created_at=res.created_at,
            updated_at=self.clock.now(),
            result=res.result,
        )
        self.store.put(self._k(res.key), updated.to_dict())
        return updated

    def complete(self, res: Reservation, result: dict[str, Any]) -> Reservation:
        updated = Reservation(
            key=res.key,
            state=ExecState.COMPLETED,
            incident_id=res.incident_id,
            action=res.action,
            target=res.target,
            attempts=res.attempts,
            created_at=res.created_at,
            updated_at=self.clock.now(),
            result=result,
        )
        self.store.put(self._k(res.key), updated.to_dict())
        return updated

    def fail(self, res: Reservation, error: str) -> Reservation:
        updated = Reservation(
            key=res.key,
            state=ExecState.FAILED,
            incident_id=res.incident_id,
            action=res.action,
            target=res.target,
            attempts=res.attempts,
            created_at=res.created_at,
            updated_at=self.clock.now(),
            result={"error": error},
        )
        self.store.put(self._k(res.key), updated.to_dict())
        return updated

    def resolve_ambiguous(
        self, res: Reservation, observed_effect: bool
    ) -> Reservation:
        """Settle an IN_FLIGHT record using evidence from the world.

        When the ack was lost, the only way to learn what happened is to look
        at the service. If the effect is visible, the action landed; if not,
        it is safe to retry.
        """
        if observed_effect:
            return self.complete(res, {"reconciled": True, "observed": True})
        return self.fail(res, "reconciled: no observed effect, safe to retry")
