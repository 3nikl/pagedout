"""
Seed sweep: run many scenarios under many seeds and collect violations.

Parallel across processes, not threads — each worker owns its own simulation
and shares nothing, so the GIL is irrelevant and determinism is unaffected.
A seed produces the same trace regardless of which worker runs it, which is
what makes parallelism safe here at all.

Usage:
    python groundhog/explore.py --seeds 20000
    python groundhog/explore.py --seeds 5000 --scenario pool_exhaustion_drain
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runner import SCENARIOS, Scenario, run  # noqa: E402
from world import FaultProfile  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmarks" / "groundhog_results.json"


@dataclass
class Finding:
    invariant: str
    scenario: str
    seed: int
    detail: str
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "invariant": self.invariant,
            "scenario": self.scenario,
            "seed": self.seed,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class SweepResult:
    runs: int = 0
    crashed: int = 0
    recovered: int = 0
    side_effects: int = 0
    wall_seconds: float = 0.0
    simulated_seconds: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    by_invariant: Counter = field(default_factory=Counter)

    @property
    def compression(self) -> float:
        return self.simulated_seconds / self.wall_seconds if self.wall_seconds else 0.0


def _chunk(args) -> dict:
    """Worker: run a contiguous block of seeds for one scenario."""
    scenario_index, lo, hi = args
    scenario = SCENARIOS[scenario_index]
    profile = FaultProfile()

    findings: list[dict] = []
    crashed = recovered = effects = 0
    simulated_ticks = 0

    for seed in range(lo, hi):
        result = run(seed, scenario, profile)
        crashed += int(result.crashed)
        recovered += int(result.recovered)
        effects += result.side_effect_count
        if result.trace.events:
            simulated_ticks += result.trace.events[-1].at
        for violation in result.violations:
            findings.append(Finding(
                invariant=violation.invariant,
                scenario=scenario.name,
                seed=seed,
                detail=violation.detail,
                evidence=violation.evidence,
            ).to_dict())

    return {
        "runs": hi - lo,
        "crashed": crashed,
        "recovered": recovered,
        "side_effects": effects,
        "simulated_ticks": simulated_ticks,
        "findings": findings,
    }


def sweep(total_seeds: int, scenarios: list[Scenario], workers: int) -> SweepResult:
    tasks = []
    block = max(1, total_seeds // (workers * 4))
    for index, scenario in enumerate(SCENARIOS):
        if scenario not in scenarios:
            continue
        for lo in range(0, total_seeds, block):
            tasks.append((index, lo, min(lo + block, total_seeds)))

    out = SweepResult()
    start = time.perf_counter()

    with mp.Pool(workers) as pool:
        for chunk in pool.imap_unordered(_chunk, tasks):
            out.runs += chunk["runs"]
            out.crashed += chunk["crashed"]
            out.recovered += chunk["recovered"]
            out.side_effects += chunk["side_effects"]
            out.simulated_seconds += chunk["simulated_ticks"] / 1_000_000
            for f in chunk["findings"]:
                out.findings.append(Finding(**f))
                out.by_invariant[f["invariant"]] += 1

    out.wall_seconds = time.perf_counter() - start
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5000)
    ap.add_argument("--scenario", type=str, default=None)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    args = ap.parse_args()

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s.name == args.scenario]
        if not scenarios:
            print(f"no scenario named {args.scenario!r}")
            return

    total_runs = args.seeds * len(scenarios)
    print("=" * 78)
    print("Groundhog seed sweep")
    print(f"  scenarios : {len(scenarios)}")
    print(f"  seeds     : {args.seeds:,} each  ->  {total_runs:,} runs")
    print(f"  workers   : {args.workers}")
    print("=" * 78)

    result = sweep(args.seeds, scenarios, args.workers)

    print(f"\n  runs                 : {result.runs:,}")
    print(f"  wall clock           : {result.wall_seconds:.1f}s")
    print(f"  runs/sec             : {result.runs / result.wall_seconds:,.0f}")
    print(f"  simulated time       : {result.simulated_seconds:,.0f}s "
          f"({result.simulated_seconds / 3600:,.1f} hours)")
    print(f"  time compression     : {result.compression:,.0f}x")
    print(f"  crashes injected     : {result.crashed:,}")
    print(f"  recovered            : {result.recovered:,}")
    print(f"  side effects applied : {result.side_effects:,}")

    print(f"\n  VIOLATIONS: {len(result.findings):,}")
    if result.by_invariant:
        for name, count in result.by_invariant.most_common():
            print(f"    {name:34} {count:>7,}")
        print("\n  smallest reproducing seed per invariant:")
        seen: dict[str, Finding] = {}
        for f in result.findings:
            key = f"{f.invariant}/{f.scenario}"
            if key not in seen or f.seed < seen[key].seed:
                seen[key] = f
        for key, f in sorted(seen.items()):
            print(f"    seed={f.seed:<8} {key}")
            print(f"              {f.detail}")
    else:
        print("    none — all invariants held")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "runs": result.runs,
        "wall_seconds": round(result.wall_seconds, 2),
        "runs_per_second": round(result.runs / result.wall_seconds, 1),
        "simulated_seconds": round(result.simulated_seconds, 1),
        "time_compression": round(result.compression, 1),
        "crashes": result.crashed,
        "recovered": result.recovered,
        "side_effects": result.side_effects,
        "violations": len(result.findings),
        "by_invariant": dict(result.by_invariant),
        "findings": [f.to_dict() for f in result.findings[:200]],
    }, indent=2))
    print(f"\n  written to {OUT}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
