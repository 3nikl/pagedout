"""
The simulated world: services, network and durable store.

These are drop-in replacements for the production ports. The executor cannot
tell the difference — same Protocol, same method signatures — which is the
point. We are testing the real executor, not a model of it.

What the world can do to the executor:

  network   drop a request, drop the ACK (so the effect happens but the
            caller sees a timeout), delay, or duplicate it
  crash     kill the process at an arbitrary operation count, then recover
            from the write-ahead log
  store     lose uncommitted WAL entries on crash

The ACK-drop is the most important fault in the whole simulator. It creates
the state where the side effect HAS happened and the caller cannot know it —
the exact condition that makes naive retry double-execute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sim import Crashed, EventKind, SeededRng, Trace, VirtualClock


# ── Fault configuration ───────────────────────────────────────────────────────


@dataclass
class FaultProfile:
    """Per-run probabilities. Drawn from the seeded RNG, so reproducible."""

    drop_request: float = 0.05      # never arrives
    drop_ack: float = 0.10          # arrives, reply lost  <- the dangerous one
    delay: float = 0.15             # arrives slowly
    duplicate: float = 0.05         # arrives twice
    max_delay_s: float = 3.0
    crash_probability: float = 0.15  # process dies somewhere in the run

    @staticmethod
    def quiet() -> "FaultProfile":
        return FaultProfile(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


# ── Simulated services ────────────────────────────────────────────────────────


@dataclass
class SimService:
    """A stand-in for one victim service.

    Mirrors the real app's observable state and admin endpoints closely
    enough that the executor's dry-run predictions and verify predicates
    operate on the same fields they would in production.
    """

    name: str
    pool_size: int = 20
    pool_in_use: int = 0
    heap_pct: float = 12.0
    cache_entries: int = 0
    version: str = "v2.3.0"
    previous_version: str = "v2.2.8"
    faults: set[str] = field(default_factory=set)

    # Every mutation is appended here. This is ground truth for the
    # "no action executed twice" invariant — not what the executor believes
    # happened, but what actually happened to the service.
    applied: list[str] = field(default_factory=list)

    # Server-side idempotency: key -> cached (status, body).
    #
    # GROUNDHOG FINDING (seed 149): client-side idempotency keys are
    # necessary but NOT SUFFICIENT. rollback_version is an involution, so
    # when the network duplicated the request the service ended up back in
    # its original state and no amount of client-side state inspection could
    # reveal that it had run twice. The only correct fix is for the SERVER to
    # remember keys and refuse to apply one twice — which is exactly what
    # Stripe, AWS and every other serious API does with Idempotency-Key.
    seen_keys: dict[str, tuple[int, dict]] = field(default_factory=dict)

    def health(self) -> dict[str, Any]:
        return {
            "service": self.name,
            "healthy": not self.faults,
            "version": self.version,
            "previous_version": self.previous_version,
            "pool_size": self.pool_size,
            "pool_in_use": self.pool_in_use,
            "pool_utilization": round(self.pool_in_use / max(self.pool_size, 1), 3),
            "heap_pct": round(self.heap_pct, 1),
            "cache_entries": self.cache_entries,
            "active_faults": sorted(self.faults),
        }

    def apply(
        self, path: str, body: dict[str, Any], idempotency_key: str | None = None
    ) -> tuple[int, dict[str, Any], bool]:
        """Perform an admin action.

        Returns (status, body, applied). `applied` is False when the request
        was deduplicated server-side, which lets the caller avoid recording a
        side effect that never happened.
        """
        if idempotency_key and idempotency_key in self.seen_keys:
            status, cached = self.seen_keys[idempotency_key]
            return status, {**cached, "deduplicated": True}, False

        status, body_out = self._apply_uncached(path, body)
        if idempotency_key:
            self.seen_keys[idempotency_key] = (status, body_out)
        return status, body_out, True

    def _apply_uncached(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == "/admin/pool/drain":
            before = self.pool_in_use
            self.pool_in_use = 0
            self.faults.discard("pool_exhaustion")
            self.applied.append("drain_pool")
            return 200, {"changed": before > 0, "drained": before}

        if path == "/admin/pool/resize":
            size = body.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 500:
                return 400, {"error": "size must be an int within 1..500"}
            before = self.pool_size
            self.pool_size = size
            self.applied.append(f"resize_pool:{size}")
            return 200, {"changed": before != size, "before": before, "after": size}

        if path == "/admin/cache/clear":
            before = self.cache_entries
            self.cache_entries = 0
            self.heap_pct = 12.0
            self.faults.discard("memory_leak")
            self.applied.append("clear_cache")
            return 200, {"changed": before > 0, "cleared": before}

        if path == "/admin/version/rollback":
            if self.version == self.previous_version:
                self.applied.append("rollback_version:noop")
                return 200, {"changed": False, "version": self.version}
            rolled_from = self.version
            self.version, self.previous_version = self.previous_version, rolled_from
            self.faults.discard("bad_deploy")
            self.applied.append("rollback_version")
            return 200, {"changed": True, "from": rolled_from, "to": self.version}

        return 404, {"error": f"unknown path {path}"}


# ── Simulated store ───────────────────────────────────────────────────────────


class SimStore:
    """Durable store whose crash behaviour is explicit.

    On crash, entries appended but not committed are LOST. That models a
    process dying between writing an intent and making it visible, and it is
    what turns a tidy state machine into a source of real bugs.
    """

    def __init__(self, trace: Trace, clock: VirtualClock):
        self._data: dict[str, dict[str, Any]] = {}
        self._wal: list[tuple[int, str, dict[str, Any], bool]] = []
        self._next_lsn = 1
        self.trace = trace
        self.clock = clock

    def _copy(self, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return json.loads(json.dumps(value)) if value is not None else None

    def get(self, key: str) -> dict[str, Any] | None:
        return self._copy(self._data.get(key))

    def append_intent(self, key: str, value: dict[str, Any]) -> int:
        lsn = self._next_lsn
        self._next_lsn += 1
        self._wal.append((lsn, key, self._copy(value), False))
        return lsn

    def commit(self, lsn: int) -> None:
        for i, (entry_lsn, key, value, done) in enumerate(self._wal):
            if entry_lsn == lsn and not done:
                self._data[key] = value
                self._wal[i] = (entry_lsn, key, value, True)
                return

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.commit(self.append_intent(key, value))
        self.trace.record(self.clock, EventKind.STORE, "store", "put", key=key,
                          state=value.get("state"))

    def compare_and_set(
        self, key: str, expected: dict[str, Any] | None, value: dict[str, Any]
    ) -> bool:
        if self._data.get(key) != expected:
            return False
        self._data[key] = self._copy(value)
        self.trace.record(self.clock, EventKind.STORE, "store", "cas",
                          key=key, state=value.get("state"))
        return True

    def recover(self) -> int:
        """Replay committed state; DISCARD uncommitted intents."""
        before = len(self._wal)
        self._wal = [e for e in self._wal if e[3]]
        lost = before - len(self._wal)
        self.trace.record(self.clock, EventKind.RECOVER, "store",
                          "wal recovery", uncommitted_lost=lost)
        return lost

    def crash(self) -> None:
        """Lose everything not yet committed."""
        self._wal = [e for e in self._wal if e[3]]


# ── Simulated transport ───────────────────────────────────────────────────────


class SimTransport:
    """Network with injectable faults, and a crash budget.

    Counts operations and raises Crashed when the budget runs out, which is
    how we inject a process death at a precise, reproducible point in the run.
    """

    def __init__(
        self,
        services: dict[str, SimService],
        rng: SeededRng,
        clock: VirtualClock,
        trace: Trace,
        profile: FaultProfile,
        crash_at: int | None = None,
    ):
        self.services = services
        self.rng = rng
        self.clock = clock
        self.trace = trace
        self.profile = profile
        self.crash_at = crash_at
        self.ops = 0

    def _tick(self, where: str) -> None:
        self.ops += 1
        if self.crash_at is not None and self.ops >= self.crash_at:
            self.trace.record(self.clock, EventKind.CRASH, "process",
                              f"crash at op {self.ops}", where=where)
            raise Crashed(where)

    def get(self, service: str, path: str, timeout: float = 5.0):
        from ports import Response

        self._tick(f"GET {service}{path}")
        svc = self.services.get(service)
        if svc is None:
            return Response(status=None, error="unknown service", delivered=False)

        if self.rng.chance(self.profile.drop_request):
            self.trace.record(self.clock, EventKind.FAULT, service,
                              "GET dropped", path=path)
            self.clock.sleep(timeout)
            return Response(status=None, error="timeout", delivered=False)

        self.clock.sleep(0.002)
        return Response(status=200, body=svc.health())

    def post(
        self,
        service: str,
        path: str,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float = 5.0,
    ):
        from ports import Response

        self._tick(f"POST {service}{path}")
        svc = self.services.get(service)
        if svc is None:
            return Response(status=None, error="unknown service", delivered=False)

        body = body or {}

        # 1. Request lost before arrival. No side effect.
        if self.rng.chance(self.profile.drop_request):
            self.trace.record(self.clock, EventKind.FAULT, service,
                              "request dropped", path=path)
            self.clock.sleep(timeout)
            return Response(status=None, error="timeout", delivered=False)

        # 2. Slow but arrives.
        if self.rng.chance(self.profile.delay):
            delay = self.rng.random() * self.profile.max_delay_s
            self.clock.sleep(delay)
            self.trace.record(self.clock, EventKind.FAULT, service,
                              "delayed", path=path, seconds=round(delay, 3))

        # The service performs the action. This is the real side effect and
        # it is recorded as ground truth regardless of what the caller learns.
        status, resp_body, applied = svc.apply(path, body, idempotency_key)
        if applied:
            self.trace.record(self.clock, EventKind.SIDE_EFFECT, service, path,
                              key=idempotency_key, changed=resp_body.get("changed"),
                              op=self.ops)
        else:
            self.trace.record(self.clock, EventKind.NOTE, service,
                              "deduplicated server-side", key=idempotency_key)

        # 3. Network duplicates the request: the service applies it twice.
        if self.rng.chance(self.profile.duplicate):
            _, _, dup_applied = svc.apply(path, body, idempotency_key)
            self.trace.record(self.clock, EventKind.FAULT, service,
                              "request duplicated by network", path=path,
                              key=idempotency_key, applied=dup_applied)
            if dup_applied:
                self.trace.record(self.clock, EventKind.SIDE_EFFECT, service, path,
                                  key=idempotency_key, duplicate=True, op=self.ops)

        # 4. The ACK is lost. The effect HAPPENED; the caller cannot know.
        #    This is the fault that makes idempotency necessary.
        if self.rng.chance(self.profile.drop_ack):
            self.trace.record(self.clock, EventKind.FAULT, service,
                              "ack dropped (effect applied)", path=path,
                              key=idempotency_key)
            self.clock.sleep(timeout)
            return Response(status=None, error="timeout", delivered=True)

        self.clock.sleep(0.003)
        self.trace.record(self.clock, EventKind.RESPONSE, service,
                          f"{status} {path}", key=idempotency_key)
        return Response(status=status, body=resp_body)


# ── Rng adapter ───────────────────────────────────────────────────────────────


class SimRng:
    """Adapts SeededRng to the executor's Rng port."""

    def __init__(self, rng: SeededRng):
        self._rng = rng

    def random(self) -> float:
        return self._rng.random()

    def jitter(self, base: float, factor: float = 0.1) -> float:
        return self._rng.jitter(base, factor)
