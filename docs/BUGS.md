# Bugs found by Groundhog

Every bug below was found by deterministic simulation, reproduces exactly from
its seed, and is now locked into a regression suite.

Neither was found by conventional testing, and neither would have been. Both
require a specific sequence of two independent faults; the per-run probability
is under 1%, and the state they corrupt is invisible to an assertion that only
checks the final outcome.

**Sweep after both fixes:** 600,000 runs · 214.5 simulated hours ·
117,000× time compression · **0 violations**.

---

## GH-001 — Double execution when the verification probe is also lost

| | |
|---|---|
| **Invariant** | `at_most_once` |
| **Seed** | `3` |
| **Scenario** | `pool_exhaustion_drain` |
| **Frequency** | 181 in 15,000 runs (~1.2%) |
| **Severity** | High — remediation applied twice to a live service |

### Trace

```
0.0020s  side_effect  ledger-service  /admin/pool/drain      ← APPLIED
0.0020s  fault        ledger-service  ack dropped            ← caller sees timeout
5.0020s  fault        ledger-service  GET dropped            ← verify probe ALSO lost
8.5357s  side_effect  ledger-service  /admin/pool/drain      ← APPLIED AGAIN
```

### Root cause

The executor defends against a lost acknowledgement by probing the service to
see whether the effect landed. The probe returns an empty dict when the request
fails, and the guard read:

```python
after = self._health(target)
if spec.verify and before and after and spec.verify(before, after):
    ...confirmed
# falls through to retry
```

An empty `after` fails the `and after` test, so control fell through to the
retry path. **The defence had a failure mode of its own, and that failure mode
was silently interpreted as "the action did not happen."**

The deeper error is conflating two distinct states:

- `False` — the effect definitively did not occur (retry is safe)
- `None` — we could not determine whether it occurred (retry is **not** safe)

### Fix

`_probe_effect()` returns a tristate and retries the probe with backoff. An
unobservable state now **fails closed**: the action is marked `AMBIGUOUS` and
escalated rather than retried.

```python
observed, after = self._probe_effect(spec, target, before)
if observed is True:   -> complete
if observed is None:   -> AMBIGUOUS, do not retry
# observed is False    -> definitively did not happen, retry is safe
```

### Lesson

Any mechanism that verifies an action can itself fail. "Cannot observe" must
never be collapsed into "did not happen."

---

## GH-002 — Client-side idempotency cannot protect an involution

| | |
|---|---|
| **Invariant** | `at_most_once` |
| **Seed** | `149` |
| **Scenario** | `bad_deploy_rollback` |
| **Frequency** | 95 in 100,000 runs (~0.1%) |
| **Severity** | Critical — service left on the *bad* version it was rolling away from |

### Trace

```
0.5350s  side_effect  checkout-service  /admin/version/rollback   v2.3.1 -> v2.2.8
0.5350s  fault        checkout-service  request duplicated by network
0.5350s  side_effect  checkout-service  /admin/version/rollback   v2.2.8 -> v2.3.1  ← BACK TO BAD
0.5350s  fault        checkout-service  ack dropped
         probe: version == v2.3.1 == starting state -> "nothing happened"
6.2641s  side_effect  checkout-service  /admin/version/rollback   ← retried
6.2641s  fault        checkout-service  request duplicated by network
6.2641s  side_effect  checkout-service  /admin/version/rollback
```

### Root cause

`rollback_version` is an **involution**: applying it twice returns the system
to its starting state. When the network duplicated the request, the service
ended up back on the bad version — and from the client's perspective that is
*indistinguishable from the action never having run*.

Two separate defects compounded:

1. **The verify predicate was a difference check.** `after.version !=
   before.version` cannot detect an even number of applications, because an
   even number produces no difference. Replaced with a target-state assertion:
   `after.version == before.previous_version`.

2. **The deeper problem: client-side idempotency keys are necessary but not
   sufficient.** Once the *network* can duplicate a request, no amount of
   client-side state inspection can determine how many times the server
   applied it. Fixing the predicate was not enough — the sweep still reported
   95 violations afterward.

### Fix

The **server** must honour the idempotency key. Both `SimService` and the real
victim app now cache responses by `Idempotency-Key` and refuse to apply a key
twice, returning the original response with `deduplicated: true`.

This is why Stripe, AWS and every other serious API put idempotency on the
server side rather than trusting clients to retry carefully.

### Lesson

Two lessons, and the second is the more valuable:

1. A difference check cannot verify a self-inverse operation.
2. **Client-side idempotency is a client-side optimisation, not a correctness
   guarantee.** At-most-once semantics require server participation. A retry
   policy alone cannot provide it.

---

## Why conventional testing missed both

| | Integration suite | Groundhog |
|---|---|---|
| Runs | 200 | 600,000 |
| Network faults | none | drop, delay, duplicate, ack-loss |
| Crash injection | none | 23,199 crashes + recoveries |
| Reproducibility | timing-dependent | exact, from a 64-bit seed |
| Wall clock | minutes | 6.6 seconds |

GH-001 needs an ack-drop followed by a probe-drop. GH-002 needs a duplicate
followed by an ack-drop. Both are compound faults with sub-1% probability, and
both leave the system in a state where the *final outcome looks correct* —
GH-002 in particular ends with the service running a valid version, just the
wrong one.

An assertion on the end state would pass. Only an invariant checked against
ground truth — what the service actually did, not what the executor believed —
catches them.

---

## Regression suite

Both seeds are pinned in `groundhog/test_regressions.py`. They run in under a
second and fail loudly if either fix is ever reverted.

```bash
python groundhog/test_regressions.py
```
