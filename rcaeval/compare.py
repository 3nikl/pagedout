"""
PagedOut's cascade attribution vs BARO, on identical, correctly-prepared cases.

A first attempt at this comparison scored BARO at 21% AC@1 — far below its
published performance. That was a harness bug, not a result. RCAEval's own
`main.py` windows each case to +/- `length` minutes around the injection and
runs `preprocess()` before handing data to a method; I had been passing the
full 70-minute series, which dilutes change-point detection.

This version reproduces their pipeline exactly:

    normal = rows before inject, last  (length * 60 // 2) samples
    anomal = rows at/after inject, first (length * 60 // 2) samples
    data   = concat(normal, anomal)
    data   = preprocess(data, dataset=...)   # drop_time, drop_constant, mem->MB

and applies it to ALL THREE rankers, so nobody is advantaged. Publishing a
comparison where a peer-reviewed method underperforms because of my own
harness would be worse than publishing no comparison at all.

Usage:
    python rcaeval/compare.py --dataset RE1-OB --length 20
"""

from __future__ import annotations

import argparse
import json
import warnings
from collections import defaultdict
from pathlib import Path

import pandas as pd

from attribute import TOPOLOGIES, anomaly_scores, rank_cascade, rank_flat

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE.parent / "docs" / "benchmarks" / "rcaeval_results.json"

# RCAEval's dataset key for Online Boutique, as main.py passes it to preprocess.
PREPROCESS_KEY = {"RE1-OB": "online-boutique", "RE1-SS": "sock-shop"}


def prepare(df: pd.DataFrame, inject_time: int, length: int) -> pd.DataFrame:
    """RCAEval main.py's windowing. Samples are 2 seconds apart."""
    n = length * 60 // 2
    normal = df[df.time < inject_time].tail(n)
    anomal = df[df.time >= inject_time].head(n)
    return pd.concat([normal, anomal], ignore_index=True)


def baro_rank(df: pd.DataFrame, inject_time: int, key: str) -> list[str]:
    """BARO's ranking, collapsed from service_metric pairs to services."""
    from RCAEval.e2e.baro import baro
    from RCAEval.io.time_series import preprocess

    prepared = preprocess(df.copy(), dataset=key, dk_select_useful=False)
    # preprocess drops `time`, which baro needs for the change point search.
    prepared["time"] = df["time"].to_numpy()

    try:
        pairs = baro(prepared, inject_time)["ranks"]
    except Exception as exc:
        print(f"    baro failed: {type(exc).__name__}: {exc}")
        return []

    seen: list[str] = []
    for p in pairs:
        svc = p.rsplit("_", 1)[0]
        if svc not in seen:
            seen.append(svc)
    return seen


def run(dataset: str, length: int, limit: int | None = None) -> dict:
    index = pd.read_parquet(HERE / "cases.parquet")
    cases = index[index.dataset == dataset]
    if limit:
        cases = cases.head(limit)

    names = ["flat", "cascade", "baro"]
    hits = {n: defaultdict(int) for n in names}
    by_fault = {n: defaultdict(lambda: defaultdict(int)) for n in names}
    fault_n: dict[str, int] = defaultdict(int)
    evaluated = skipped = 0

    for i, (_, row) in enumerate(cases.iterrows(), 1):
        mfile = DATA / row.case / "metrics.parquet"
        if not mfile.exists():
            skipped += 1
            continue

        raw = pd.read_parquet(mfile)
        t = int(row.inject_time)
        windowed = prepare(raw, t, length)

        # Identical input for every ranker.
        scores = anomaly_scores(windowed, t)
        br = baro_rank(windowed, t, PREPROCESS_KEY.get(dataset, 'online-boutique'))
        if not scores or not br:
            skipped += 1
            continue

        topo = TOPOLOGIES.get(dataset)
        ranks = {"flat": rank_flat(scores),
                 "cascade": rank_cascade(scores, topo), "baro": br}

        evaluated += 1
        fault_n[row.fault] += 1
        truth = row.root_cause_service
        for n in names:
            for k in (1, 2, 3, 4, 5):
                hit = int(truth in ranks[n][:k])
                hits[n][k] += hit
                if k == 1:
                    by_fault[n][row.fault]["top1"] += hit
                if k == 5:
                    by_fault[n][row.fault]["top5"] += hit

        if i % 25 == 0:
            print(f"  {i}/{len(cases)}")

    if not evaluated:
        raise SystemExit("no evaluable cases")

    result = {
        "dataset": dataset,
        "window_minutes": length,
        "cases_evaluated": evaluated,
        "cases_skipped": skipped,
        "note": ("all rankers see identical windowed+preprocessed input, "
                 "reproducing RCAEval main.py"),
        "rankers": {},
        "by_fault": {},
    }
    for n in names:
        acs = {f"AC@{k}": round(hits[n][k] / evaluated, 4) for k in (1, 2, 3, 4, 5)}
        acs["Avg@5"] = round(sum(acs[f"AC@{k}"] for k in (1, 2, 3, 4, 5)) / 5, 4)
        result["rankers"][n] = acs
        result["by_fault"][n] = {
            f: {"n": fault_n[f],
                "AC@1": round(by_fault[n][f]["top1"] / fault_n[f], 4),
                "AC@5": round(by_fault[n][f]["top5"] / fault_n[f], 4)}
            for f in sorted(fault_n)
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="RE1-OB")
    ap.add_argument("--length", type=int, default=20, help="window, minutes each side")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    r = run(args.dataset, args.length, args.limit)

    print("\n" + "=" * 72)
    print(f"RCAEval {r['dataset']} — {r['cases_evaluated']} cases, "
          f"±{r['window_minutes']}min window, identical inputs")
    print("=" * 72)
    print(f"\n{'ranker':14} {'AC@1':>8} {'AC@2':>8} {'AC@3':>8} {'AC@5':>8} {'Avg@5':>8}")
    print("-" * 72)
    label = {"flat": "flat", "cascade": "cascade *", "baro": "BARO (FSE'24)"}
    for n in ("flat", "cascade", "baro"):
        a = r["rankers"][n]
        print(f"{label[n]:14} " + " ".join(
            f"{a[m]:>8.1%}" for m in ("AC@1", "AC@2", "AC@3", "AC@5", "Avg@5")))
    print("-" * 72)
    print("* PagedOut's cascade attribution")

    print(f"\n{'fault':9} {'n':>4}  {'flat':>8} {'cascade':>9} {'BARO':>8}   (AC@1)")
    print("-" * 52)
    for f in r["by_fault"]["cascade"]:
        print(f"{f:9} {r['by_fault']['cascade'][f]['n']:>4}  "
              f"{r['by_fault']['flat'][f]['AC@1']:>8.1%} "
              f"{r['by_fault']['cascade'][f]['AC@1']:>9.1%} "
              f"{r['by_fault']['baro'][f]['AC@1']:>8.1%}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(r, indent=2))
    print(f"\n  written to {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
