# PagedOut on RCAEval

Everything else in this repository is measured against telemetry PagedOut
generated itself. This is the exception: an evaluation on
[RCAEval](https://github.com/phamquiluan/RCAEval), a public root-cause-analysis
benchmark published at FSE'26, WWW'25 and ASE'24, using data somebody else
collected and ground truth somebody else annotated.

**Result: 91.1% AC@1 across 248 cases, against 81.0% for BARO on identical
inputs.**

---

## What is being tested

PagedOut's investigator does not rank services by how loudly they are failing.
It asks a structural question:

> Is this service broken, or is it merely downstream of something broken?

A service that is anomalous *and* depends on another anomalous service is
probably a victim. The root cause is the anomalous service with no anomalous
dependency beneath it. In the live system that check runs against `/health`
endpoints and a declared topology
([`agents/tools.py`](../agents/tools.py)).

RCAEval provides time-series metrics and nothing else, so the principle
transfers but the implementation cannot. In [`rcaeval/attribute.py`](../rcaeval/attribute.py):

- **anomalous** becomes a change statistic across the injection boundary —
  `|median_after − median_before| / MAD_before`, per metric, max over each
  service's metrics
- **topology** becomes the published call graph of the system under test

Three rankers, identical inputs:

| | |
|---|---|
| `flat` | rank by anomaly score alone — blame whatever changed most |
| `cascade` | the same scores, demoted when a dependency is also anomalous |
| `BARO` | RCAEval's published baseline (FSE 2024), run locally |

`flat` exists so the contribution of the structural step is isolated. If
cascade attribution is worth anything, it beats `flat`.

---

## Results

RE1 suite, metrics-only, ±20-minute window around injection, 248 cases.

### Pooled

| ranker | AC@1 | AC@3 | AC@5 | Avg@5 |
|---|---|---|---|---|
| flat | 87.1% | 97.2% | 99.6% | 95.4% |
| **cascade** | **91.1%** | 96.8% | 99.6% | **95.7%** |
| BARO (FSE'24) | 81.0% | 96.0% | 98.8% | 93.3% |

### RE1-OB — Online Boutique, 123 cases

| ranker | AC@1 | AC@3 | Avg@5 |
|---|---|---|---|
| flat | 87.0% | 94.3% | 93.8% |
| **cascade** | **88.6%** | 93.5% | 93.5% |
| BARO | 78.0% | 93.5% | 91.1% |

### RE1-SS — Sock Shop, 125 cases

| ranker | AC@1 | AC@3 | Avg@5 |
|---|---|---|---|
| flat | 87.2% | 100.0% | 97.0% |
| **cascade** | **93.6%** | 100.0% | **97.9%** |
| BARO | 84.0% | 98.4% | 95.5% |

### By fault type — AC@1

| fault | OB flat → cascade | SS flat → cascade |
|---|---|---|
| cpu | 95.8% → 95.8% | 100.0% → 100.0% |
| mem | 100.0% → 100.0% | 92.0% → 96.0% |
| disk | 92.0% → **96.0%** | 88.0% → 88.0% |
| delay | 92.0% → **96.0%** | 88.0% → **100.0%** |
| loss | 54.2% → 54.2% | 68.0% → **84.0%** |

---

## Reading the result honestly

**The structural step earns its place, but only where there is structure.**
Cascade attribution adds +1.6 points on Online Boutique and +6.4 on Sock Shop.
That difference is the finding. Sock Shop puts every business service in front
of its own database, so `front-end → carts → carts-db` is a genuine two-hop
cascade. Online Boutique is shallower and there is less for the structural
step to disentangle.

**It helps on propagating faults and not on local ones.** `cpu` and `mem`
faults barely move: a CPU stress on a service spikes that service's own CPU
metric, so there is no attribution problem to solve. The gains are on `delay`
and `loss` — network faults that light up every service on the path while the
actual fault sits at one end of it. That is exactly the case the principle was
designed for.

**`loss` is the hard one and remains hard.** 54% on Online Boutique even after
the structural step. Packet loss does not reliably perturb any single service's
own metrics, which is a real limitation, not a tuning problem.

---

## What this does not show

Stated plainly, because these caveats matter more than the headline.

**This is RE1 only — 2 of 9 datasets, 248 of 735 cases.** RE1 is metrics-only
and the easiest suite. RE2 adds logs and traces; RE3 covers code-level faults.

**Train Ticket was not run.** It is the hard system — 40+ services against
Online Boutique's 12 and Sock Shop's 15 — and a ranking metric is far more
forgiving with a smaller candidate set. Cascade attribution needs a declared
topology and I did not build Train Ticket's. Expect all three numbers to fall
there, and the gap between them to be the interesting part.

**Cascade attribution gets the dependency graph for free. BARO does not.**
This is a real methodological advantage and it should be disclosed rather than
buried: BARO infers structure from the data, while `cascade` is handed the
published call graph. A fair reading is that supplying known topology is worth
roughly 10 points of AC@1 on these systems — which is a useful thing to know,
not a claim of algorithmic superiority.

**BARO's published figure is on a different dataset.** The paper reports
Avg@5 ≈ 0.80 on RE2-TT. The numbers above are from running BARO locally on
RE1-OB and RE1-SS through RCAEval's own preprocessing, so they are comparable
to `flat` and `cascade` but **not** to the paper's table.

**No parameter was tuned against the benchmark.** `DEMOTION = 1.0` was fixed
before the first evaluation run and never changed.

---

## A harness bug worth recording

The first version of this comparison scored BARO at **21% AC@1** — far below
its published performance. That was not a result, it was a bug.

RCAEval's `main.py` windows each case to ±20 minutes around the injection and
runs `preprocess()` before handing data to a method. I had been passing the
full 70-minute series, which dilutes change-point detection. After reproducing
their pipeline, BARO scored 78–84%, in line with what the paper reports.

The rule that caught it: when a peer-reviewed method performs terribly in your
harness, suspect the harness. Publishing that first number would have been
both wrong and embarrassing.

---

## Reproducing

```bash
pip install "RCAEval[default]" pandas pyarrow huggingface_hub

python - <<'EOF'
from huggingface_hub import snapshot_download
for suite in ("re1ob*", "re1ss*"):
    snapshot_download(repo_id="phamquiluan/RCAEval", repo_type="dataset",
                      allow_patterns=suite, local_dir="rcaeval/data")
EOF

python rcaeval/compare.py --dataset RE1-OB --length 20
python rcaeval/compare.py --dataset RE1-SS --length 20
```

About 64 MB of telemetry and roughly two minutes per dataset. Results land in
[`docs/benchmarks/rcaeval_results.json`](benchmarks/rcaeval_results.json).

RCAEval is MIT licensed. If you use it, cite the authors:

> Luan Pham et al. *RCAEval: A Benchmark for Root Cause Analysis of
> Microservice Systems.* WWW 2025 / ASE 2024 / FSE 2026.
