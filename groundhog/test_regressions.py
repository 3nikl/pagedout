"""
Regression suite: the exact seeds that once exposed real bugs.

This is the payoff of determinism. A bug found by simulation is not just
"fixed" — the precise scenario that produced it becomes a permanent test that
runs in milliseconds and cannot flake, because the seed reproduces the
interleaving exactly.

Conventional fault-injection tests cannot do this. They rely on timing, so a
fixed bug that regresses only reappears when the timing happens to line up
again.

Run:
    python groundhog/test_regressions.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runner import SCENARIOS, run, verify_determinism  # noqa: E402

# (seed, scenario name, bug id, what it used to do)
REGRESSIONS = [
    (3, "pool_exhaustion_drain", "GH-001",
     "ack lost AND verify probe lost -> drain executed twice"),
    (149, "bad_deploy_rollback", "GH-002",
     "network duplicate on an involution -> rollback executed twice"),
]

DETERMINISM_SEEDS = list(range(1, 101))


def _scenario(name: str):
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(name)


def test_regressions() -> int:
    failures = 0
    print("Regression seeds")
    for seed, scenario_name, bug_id, description in REGRESSIONS:
        result = run(seed, _scenario(scenario_name))
        ok = not result.violations
        print(f"  {'PASS' if ok else 'FAIL'}  {bug_id}  seed={seed:<5} "
              f"{scenario_name}")
        print(f"        {description}")
        if not ok:
            failures += 1
            for v in result.violations:
                print(f"        !! {v}")
    return failures


def test_determinism() -> int:
    """The property everything else depends on."""
    failures = 0
    print("\nDeterminism")
    for scenario in SCENARIOS:
        ok, message = verify_determinism(scenario, DETERMINISM_SEEDS)
        print(f"  {'PASS' if ok else 'FAIL'}  {message}")
        if not ok:
            failures += 1
    return failures


def test_no_violations_at_scale(seeds: int = 5000) -> int:
    """A quick sweep, so a regression that only shows at scale still fails CI."""
    print(f"\nSweep ({seeds:,} seeds x {len(SCENARIOS)} scenarios)")
    total = 0
    for scenario in SCENARIOS:
        violations = 0
        for seed in range(seeds):
            violations += len(run(seed, scenario).violations)
        total += violations
        print(f"  {'PASS' if violations == 0 else 'FAIL'}  "
              f"{scenario.name:28} {violations} violation(s)")
    return 1 if total else 0


if __name__ == "__main__":
    failed = test_regressions() + test_determinism() + test_no_violations_at_scale()
    print()
    if failed:
        print(f"FAILED: {failed} check(s)")
        sys.exit(1)
    print("All checks passed.")
