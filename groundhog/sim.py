"""
Groundhog simulation primitives: virtual clock, seeded RNG, event trace.

The contract this file exists to enforce:

    run(seed=N) must produce a byte-identical trace, every time, forever,
    on any machine.

Everything nondeterministic in the system under test routes through one of
these objects, and every one of them derives from a single 64-bit seed. If
any code path reaches `time.time()` or the `random` module directly, the
guarantee is void — which is why remediation/ports.py exists and why the
executor imports neither.

Determinism hazards deliberately handled here:

  * blake2b instead of hash()  — Python's hash() is per-process randomised
  * one RNG stream, not several — separate streams consumed in varying order
                                  would desynchronise between runs
  * float time from an integer tick counter — repeated float addition
                                  accumulates differently under different
                                  interleavings; integer ticks do not
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from random import Random
from typing import Any

# Logical time is counted in integer microseconds and only converted to float
# on read. Accumulating float additions would make time itself depend on the
# order of additions, which is precisely the nondeterminism we are removing.
TICKS_PER_SECOND = 1_000_000


class Crashed(Exception):
    """Raised to unwind the process when a crash is injected.

    An exception rather than a flag: a crash must abandon the current call
    stack the way a real process death does. Returning an error code would
    let the executor run its cleanup, which a real crash never does.
    """

    def __init__(self, at: str):
        self.at = at
        super().__init__(f"process crashed at {at}")


class VirtualClock:
    """Logical time. Never reads the wall clock.

    `sleep` advances the clock instantly rather than blocking, which is where
    the time compression comes from: a scenario with 30 seconds of retry
    backoff costs zero wall-clock time to simulate.
    """

    def __init__(self, start: float = 0.0):
        self._ticks = int(start * TICKS_PER_SECOND)

    def now(self) -> float:
        return self._ticks / TICKS_PER_SECOND

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._ticks += int(seconds * TICKS_PER_SECOND)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)

    @property
    def ticks(self) -> int:
        return self._ticks


class SeededRng:
    """The single source of all nondeterminism in a run.

    One stream, not several. Separate streams for network faults, crash
    timing and backoff jitter would look tidier, but any change in the ORDER
    components draw from them would desynchronise the run — so a code change
    in one component silently changes the scenario another component sees.
    One stream makes that impossible to get subtly wrong.
    """

    def __init__(self, seed: int):
        self.seed = seed
        self._r = Random(seed)
        self.draws = 0

    def random(self) -> float:
        self.draws += 1
        return self._r.random()

    def chance(self, probability: float) -> bool:
        return self.random() < probability

    def randint(self, low: int, high: int) -> int:
        self.draws += 1
        return self._r.randint(low, high)

    def choice(self, items: list) -> Any:
        self.draws += 1
        return items[self._r.randrange(len(items))]

    def jitter(self, base: float, factor: float = 0.1) -> float:
        spread = base * factor
        return max(0.0, base + (self.random() * 2 - 1) * spread)


class EventKind(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    FAULT = "fault"
    STORE = "store"
    CRASH = "crash"
    RECOVER = "recover"
    SIDE_EFFECT = "side_effect"
    NOTE = "note"


@dataclass(frozen=True)
class Event:
    at: int                      # logical ticks, integer for exact comparison
    kind: EventKind
    actor: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple:
        """Canonical form used for byte-identical trace comparison."""
        return (
            self.at,
            self.kind.value,
            self.actor,
            self.detail,
            json.dumps(self.data, sort_keys=True, separators=(",", ":")),
        )


class Trace:
    """Ordered log of everything that happened in one run.

    Two runs of the same seed must produce equal traces. `digest` gives a
    cheap equality check; `events` gives the detail needed to debug a failure
    and to shrink it.
    """

    def __init__(self, seed: int):
        self.seed = seed
        self.events: list[Event] = []
        self.side_effects: list[dict[str, Any]] = []

    def record(
        self,
        clock: VirtualClock,
        kind: EventKind,
        actor: str,
        detail: str,
        **data: Any,
    ) -> None:
        self.events.append(Event(clock.ticks, kind, actor, detail, data))
        if kind is EventKind.SIDE_EFFECT:
            self.side_effects.append({"at": clock.ticks, "actor": actor,
                                      "detail": detail, **data})

    def digest(self) -> str:
        import hashlib

        h = hashlib.blake2b(digest_size=16)
        for event in self.events:
            h.update(repr(event.key()).encode())
        return h.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "digest": self.digest(),
            "event_count": len(self.events),
            "events": [
                {"at": e.at, "kind": e.kind.value, "actor": e.actor,
                 "detail": e.detail, "data": e.data}
                for e in self.events
            ],
            "side_effects": self.side_effects,
        }

    def render(self, limit: int = 60) -> str:
        lines = [f"trace seed={self.seed} digest={self.digest()[:16]} "
                 f"events={len(self.events)}"]
        for e in self.events[:limit]:
            lines.append(
                f"  {e.at / TICKS_PER_SECOND:9.4f}s  {e.kind.value:12} "
                f"{e.actor:18} {e.detail}"
            )
        if len(self.events) > limit:
            lines.append(f"  ... {len(self.events) - limit} more")
        return "\n".join(lines)
