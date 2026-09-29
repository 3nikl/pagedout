"""
Injectable ports — the seam between the executor and the outside world.

This is the single most important file in the project, and it is the one that
has to exist before any executor code is written.

Deterministic simulation only works if the system under test cannot observe
anything the simulator does not control. Every call to `time.time()`, every
`random.random()`, every socket, every disk write is a source of
nondeterminism. If even one of them stays hardcoded, a failing seed will not
reproduce, and the entire second half of this project is worthless.

So the executor never imports `time`, `random`, `httpx` or `psycopg`. It
receives a Clock, an Rng, a Transport and a Store. In production those are
backed by the real thing; under Groundhog they are backed by a virtual clock,
a seeded PRNG, a lossy simulated network and a crashable store.

Protocols rather than base classes: the executor depends on the interface,
and neither implementation has to import the other.
"""

from __future__ import annotations

import hashlib
import json
import random as _random
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Responses ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Response:
    """What a Transport returns.

    `delivered` distinguishes the two cases that matter most for correctness:

        status=200, delivered=True   the call arrived and we know it
        status=None, delivered=True  the call ARRIVED but the ack was lost

    That second case is the whole reason idempotency exists. The caller cannot
    tell it apart from "never arrived", so it must retry, and the retry must
    not perform the action twice.
    """

    status: int | None
    body: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    delivered: bool = True

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def ambiguous(self) -> bool:
        """True when we do not know whether the side effect happened."""
        return self.status is None


class TransportError(Exception):
    """Raised for a failure the caller may retry."""


# ── Protocols ─────────────────────────────────────────────────────────────────


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Seconds. Monotonic within a run; not necessarily wall clock."""

    def sleep(self, seconds: float) -> None:
        """Advance time. Under simulation this yields to the scheduler."""


@runtime_checkable
class Rng(Protocol):
    def random(self) -> float:
        """Uniform in [0, 1)."""

    def jitter(self, base: float, factor: float = 0.1) -> float:
        """Randomised backoff delay.

        Lives on the port rather than in the executor so that retry timing is
        part of what the simulator controls. Retry timing changes
        interleavings, which is exactly what we want to explore.
        """


@runtime_checkable
class Transport(Protocol):
    def post(
        self,
        service: str,
        path: str,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float = 5.0,
    ) -> Response:
        ...

    def get(self, service: str, path: str, timeout: float = 5.0) -> Response:
        ...


@runtime_checkable
class Store(Protocol):
    """Durable key-value store with an explicit write-ahead log.

    The WAL is exposed rather than hidden because the interesting crash window
    is *between* appending an intent and applying it. A store that only offered
    `put` would hide the very bug we are hunting.
    """

    def get(self, key: str) -> dict[str, Any] | None: ...

    def append_intent(self, key: str, value: dict[str, Any]) -> int:
        """Record that we are about to write. Returns a log sequence number."""

    def commit(self, lsn: int) -> None:
        """Make the intent at `lsn` visible to readers."""

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Convenience: append + commit. NOT crash-safe as a unit."""

    def compare_and_set(
        self, key: str, expected: dict[str, Any] | None, value: dict[str, Any]
    ) -> bool:
        """Atomic conditional write. Returns False if `expected` did not match.

        This is the primitive that makes idempotency reservation safe. Without
        it, two concurrent workers both read "no record", both decide to act,
        and the action executes twice — a check-then-act race.
        """

    def recover(self) -> int:
        """Replay uncommitted WAL entries after a crash. Returns count applied."""


# ── Production implementations ────────────────────────────────────────────────


class SystemClock:
    def now(self) -> float:
        return _time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


class SystemRng:
    def __init__(self, seed: int | None = None):
        self._rng = _random.Random(seed)

    def random(self) -> float:
        return self._rng.random()

    def jitter(self, base: float, factor: float = 0.1) -> float:
        spread = base * factor
        return max(0.0, base + (self._rng.random() * 2 - 1) * spread)


class HttpTransport:
    """Real HTTP against the victim services.

    Imports httpx lazily so that the simulator — which never uses this class —
    does not need it installed, and so that an accidental import in simulated
    code fails loudly rather than silently reaching the network.
    """

    def __init__(self, ports: dict[str, int], host: str = "localhost"):
        self.ports = ports
        self.host = host

    def _url(self, service: str, path: str) -> str:
        port = self.ports.get(service)
        if port is None:
            raise TransportError(f"unknown service {service!r}")
        return f"http://{self.host}:{port}{path}"

    def post(
        self,
        service: str,
        path: str,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float = 5.0,
    ) -> Response:
        import httpx

        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = httpx.post(self._url(service, path), json=body or {},
                           headers=headers, timeout=timeout)
            return Response(status=r.status_code, body=_safe_json(r))
        except httpx.TimeoutException:
            # Timed out. The request may well have been processed; we simply
            # do not know. Ambiguous, not failed.
            return Response(status=None, error="timeout", delivered=True)
        except httpx.HTTPError as exc:
            return Response(status=None, error=str(exc), delivered=False)

    def get(self, service: str, path: str, timeout: float = 5.0) -> Response:
        import httpx

        try:
            r = httpx.get(self._url(service, path), timeout=timeout)
            return Response(status=r.status_code, body=_safe_json(r))
        except httpx.HTTPError as exc:
            return Response(status=None, error=str(exc), delivered=False)


def _safe_json(resp) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"value": data}
    except ValueError:
        return {}


class InMemoryStore:
    """Durable-enough store for production single-node use, and the reference
    implementation of the WAL semantics the simulator mimics.

    A real deployment would back this with Postgres; the semantics that matter
    for correctness — append, commit, compare-and-set, recover — are the same.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._wal: list[tuple[int, str, dict[str, Any], bool]] = []
        self._next_lsn = 1
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    def append_intent(self, key: str, value: dict[str, Any]) -> int:
        with self._lock:
            lsn = self._next_lsn
            self._next_lsn += 1
            self._wal.append((lsn, key, json.loads(json.dumps(value)), False))
            return lsn

    def commit(self, lsn: int) -> None:
        with self._lock:
            for i, (entry_lsn, key, value, done) in enumerate(self._wal):
                if entry_lsn == lsn and not done:
                    self._data[key] = value
                    self._wal[i] = (entry_lsn, key, value, True)
                    return

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.commit(self.append_intent(key, value))

    def compare_and_set(
        self, key: str, expected: dict[str, Any] | None, value: dict[str, Any]
    ) -> bool:
        with self._lock:
            current = self._data.get(key)
            if current != expected:
                return False
            self._data[key] = json.loads(json.dumps(value))
            return True

    def recover(self) -> int:
        with self._lock:
            applied = 0
            for i, (lsn, key, value, done) in enumerate(self._wal):
                if not done:
                    self._data[key] = value
                    self._wal[i] = (lsn, key, value, True)
                    applied += 1
            return applied


# ── Helpers ───────────────────────────────────────────────────────────────────


def stable_hash(*parts: Any) -> str:
    """Deterministic hash across processes and machines.

    Python's built-in hash() is randomised per process unless PYTHONHASHSEED is
    fixed, so using it here would mean idempotency keys differ between the
    simulator and production — and between two runs of the same seed. blake2b
    is stable everywhere.
    """
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


@dataclass
class Ports:
    """Everything the executor needs from the outside world, in one bundle."""

    clock: Clock
    rng: Rng
    transport: Transport
    store: Store

    @classmethod
    def production(cls, service_ports: dict[str, int], seed: int | None = None) -> "Ports":
        return cls(
            clock=SystemClock(),
            rng=SystemRng(seed),
            transport=HttpTransport(service_ports),
            store=InMemoryStore(),
        )
