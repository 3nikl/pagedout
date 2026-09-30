"""
Evaluate PagedOut's cascade attribution on the RCAEval benchmark.

Reports RCAEval's standard metrics so the numbers are comparable to the
published baselines:

    AC@k    fraction of cases where the annotated root cause service is in
            the top k of the ranking
    Avg@5   mean of AC@1..AC@5, the paper's headline single figure

Two rankers run over identical inputs, so the difference between them is
attributable to the structural step and nothing else:

    flat      rank by anomaly score alone
    cascade   the same scores, demoted when a dependency is also anomalous

Usage:
    python rcaeval/evaluate.py
    python rcaeval/evaluate.py --dataset RE1-OB --by-fault
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from attribute import anomaly_scores, rank_cascade, rank_flat

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE.parent / "docs" / "benchmarks" / "rcaeval_results.json"


def ac_at_k(rank: list[str], truth: str, k: int) -> int:
    return int(truth in rank[:k])


def evaluate(dataset: str, limit: int | None = None) -> dict:
    index = pd.read_parquet(HERE / "cases.parquet")
    cases = index[index.dataset == dataset]
    if limit:
        cases = cases.head(limit)

    rankers = {"flat": rank_flat, "cascade": rank_cascade}
    hits = {name: defaultdict(int) for name in rankers}
    by_fault = {name: defaultdict(lambda: defaultdict(int)) for name in rankers}
    fault_totals: dict[str, int] = defaultdict(int)
    evaluated = 0
    skipped = 0

    for _, row in cases.iterrows():
        case_dir = DATA / row.case
        metrics = case_dir / "metrics.parquet"
        if not metrics.exists():
            skipped += 1
            continue

        df = pd.read_parquet(metrics)
        scores = anomaly_scores(df, int(row.inject_time))
        if not scores:
            skipped += 1
            continue

        evaluated += 1
        fault_totals[row.fault] += 1
        truth = row.root_cause_service

        for name, ranker in rankers.items():
            rank = ranker(scores)
            for k in (1, 2, 3, 4, 5):
                hit = ac_at_k(rank, truth, k)
                hits[name][k] += hit
                if k == 1:
                    by_fault[name][row.fault]["top1"] += hit
                if k == 5:
                    by_fault[name][row.fault]["top5"] += hit

    if not evaluated:
        raise SystemExit(f"no evaluable cases for {dataset} — is the data downloaded?")

    result = {
        "dataset": dataset,
        "cases_evaluated": evaluated,
        "cases_skipped": skipped,
        "rankers": {},
        "by_fault": {},
    }
    for name in rankers:
        acs = {f"AC@{k}": round(hits[name][k] / evaluated, 4) for k in (1, 2, 3, 4, 5)}
        acs["Avg@5"] = round(sum(acs[f"AC@{k}"] for k in (1, 2, 3, 4, 5)) / 5, 4)
        result["rankers"][name] = acs
        result["by_fault"][name] = {
            fault: {
                "n": fault_totals[fault],
                "AC@1": round(by_fault[name][fault]["top1"] / fault_totals[fault], 4),
                "AC@5": round(by_fault[name][fault]["top5"] / fault_totals[fault], 4),
            }
            for fault in sorted(fault_totals)
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="RE1-OB")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--by-fault", action="store_true")
    args = ap.parse_args()

    r = evaluate(args.dataset, args.limit)

    print("=" * 66)
    skipped = f" ({r['cases_skipped']} skipped)" if r['cases_skipped'] else ""
    print(f"RCAEval {r['dataset']} — {r['cases_evaluated']} cases{skipped}")
    print("=" * 66)
    print(f"\n{'ranker':10} {'AC@1':>8} {'AC@2':>8} {'AC@3':>8} {'AC@4':>8} "
          f"{'AC@5':>8} {'Avg@5':>8}")
    print("-" * 66)
    for name, acs in r["rankers"].items():
        print(f"{name:10} " + " ".join(f"{acs[m]:>8.1%}" for m in
              ("AC@1", "AC@2", "AC@3", "AC@4", "AC@5", "Avg@5")))
    print("-" * 66)

    flat, casc = r["rankers"]["flat"], r["rankers"]["cascade"]
    d1 = casc["AC@1"] - flat["AC@1"]
    d5 = casc["Avg@5"] - flat["Avg@5"]
    print(f"\n  cascade vs flat:  AC@1 {d1:+.1%}   Avg@5 {d5:+.1%}")

    if args.by_fault:
        print(f"\n{'fault':10} {'n':>4}   {'flat AC@1':>10} {'cascade AC@1':>13}")
        print("-" * 46)
        for fault, s in r["by_fault"]["cascade"].items():
            f1 = r["by_fault"]["flat"][fault]["AC@1"]
            print(f"{fault:10} {s['n']:>4}   {f1:>10.1%} {s['AC@1']:>13.1%}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(r, indent=2))
    print(f"\n  written to {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
