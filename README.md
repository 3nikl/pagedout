# PagedOut

### Autonomous incident remediation, validated by deterministic simulation testing

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![Flink](https://img.shields.io/badge/Apache%20Flink-1.20-e6526f?logo=apacheflink)
![Kafka](https://img.shields.io/badge/Kafka-3.8%20KRaft-231f20?logo=apachekafka)
![Qdrant](https://img.shields.io/badge/Qdrant-1.12-dc244c)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-1c3c3c)
![Groundhog](https://img.shields.io/badge/Groundhog-DST-6ea8fe)

**Two halves.** PagedOut ingests telemetry, correlates it into incidents, and
plans remediation with an LLM agent graph. **Groundhog** is the deterministic
fault simulator built to break it — and it found two real safety bugs that a
conventional test suite never would.

> Every number below was measured on an 8 GB M-series MacBook. Nothing here is
> aspirational; if a thing is not built, it says so in
> [What is not built](#what-is-not-built).

---

## Watch it

![PagedOut — seed 3, an automated repair executing twice](demo/hook.gif)

Above: the opening of the launch video. Seed 3, replayed — an automated pool
drain runs, the acknowledgement is lost, the verification probe is lost too,
and the repair executes a second time. That is bug **GH-001**, reproduced
exactly from a 64-bit seed.

**▶ [Full 21-second video](demo/demo.mp4)** · [how it was made](demo/video-plan.md)

---

## Results

| | |
|---|---|
| **Ingestion** | 48,000 events/sec sustained at 100% delivery, p99 **282 ms**; latency knee at 64K (p99 degrades 7x to 1.97s) |
| **Correlation** | **63.5 : 1** signal-to-incident collapse, 30s event-time tumbling windows |
| **Checkpointing** | 14 completed / 0 failed, 138 KB state, 89 ms, RocksDB |
| **Retrieval** | Recall@3 **67.7% → 90.6%**, MRR 0.609 → 0.813, over 1,342 real documents |
| **Simulation** | **600,000** fault interleavings · 214.5 simulated hours · **117,000× compression** · 6.6 s wall clock |
| **Bugs found** | **2**, both fixed, both pinned as regression seeds — **0 found by the 200-run integration suite** |
| **Public benchmark** | **91.1% AC@1** on RCAEval (248 cases) vs 81.0% for BARO on identical inputs — [details](docs/BENCHMARK.md) |

---

## The interesting part

PagedOut takes destructive actions on infrastructure: drain a pool, clear a
cache, roll back a deploy. The dangerous failures are not "the model chose a
bad action." They are concurrency and partial failure:

- a dropped ack causes a retry causes a **double restart**
- a checkpoint recovers and **replays an action that already ran**
- a rollback **interleaves** with an in-flight remediation

Each needs one specific interleaving out of millions. A 200-run integration
suite will never reach them.

So the system is tested by a **deterministic simulator**. Virtual clock, one
seeded PRNG, single-threaded scheduling, and a network that drops, delays and
duplicates. Every failure reproduces exactly from a 64-bit seed.

It found two bugs. Both are written up in **[docs/BUGS.md](docs/BUGS.md)**.

### GH-001 — the defence had its own failure mode

The executor guards a lost ack by probing the service. The probe can fail too.
When the network dropped both, an empty health response failed the guard and
control fell through to retry — executing the action twice.

> **Lesson:** "cannot observe" must never collapse into "did not happen."

### GH-002 — client-side idempotency cannot protect an involution

`rollback_version` applied twice returns to the starting version. A
network-duplicated request left the service back on the **bad** version,
indistinguishable from never having run. Fixing the verify predicate was not
enough — 95 violations remained.

> **Lesson:** at-most-once requires **server** participation. Client-side keys
> are an optimisation, not a correctness guarantee. This is why Stripe and AWS
> put idempotency on the server.

---

## Architecture

```
┌── VICTIM APPLICATION ──────────────────────────────────────────────┐
│  checkout-service ──► payment-service ──► ledger-service           │
│  6 injectable faults · 4 real remediation endpoints · /metrics     │
└───────────┬──────────────────────────────────┬─────────────────────┘
            │ Prometheus scrape (5s)           │ JSON logs → Fluent Bit
            ▼                                  ▼
      ┌───────────┐                   Kafka  logs.raw · metrics.raw · alerts.raw
      │Prometheus │                              │
      │ + Grafana │                              ▼
      └───────────┘                   ┌─────────────────────────────┐
                                      │  Apache Flink 1.20          │
                                      │  normalize → 30s tumbling   │
                                      │  window → correlate         │
                                      │  RocksDB · exactly-once     │
                                      └──────────┬──────────────────┘
                                                 ▼
                                        incidents.correlated
                                                 │
┌── AGENT GRAPH (LangGraph) ──────────────────────▼──────────────────┐
│  triage ──<0.5──► escalate                                         │
│     │                                                              │
│     └──► investigator ──► runbook ──► planner ──► remediate ──► PM │
│           (live Prometheus)  (hybrid RAG)  (schema)   (executor)   │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │
┌── GROUNDHOG ──────────────────────▼────────────────────────────────┐
│  virtual clock · seeded PRNG · lossy network · crash injection     │
│  5 invariants checked against ground truth                         │
│  run(seed) → byte-identical trace, every time                      │
└────────────────────────────────────────────────────────────────────┘
```

---

## Quick start

Requires Docker, Python 3.11+, and [Ollama](https://ollama.com).

**Groundhog needs none of that.** It is stdlib-only and runs anywhere:

```bash
python groundhog/test_regressions.py       # determinism + regression seeds
python groundhog/explore.py --seeds 20000  # 100k interleavings, ~2s
```

For the full stack:

```bash
docker compose up -d                     # core: Kafka, Qdrant, Postgres, Redis
docker compose --profile victim up -d    # + victim app, Prometheus, Grafana
ollama pull phi3:mini
pip install -r requirements.txt
python rag/index.py                      # build the 1,342-doc index
uvicorn api.main:app --port 8000         # dashboard at http://localhost:8000
```

Compose uses **profiles** because the full stack does not fit a default
3.8 GB Docker VM. Add `--profile flink` for the ingestion pipeline,
`--profile all` for everything (needs ~6 GB).

| | |
|---|---|
| Dashboard | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Grafana | http://localhost:3000 |
| Flink UI | http://localhost:8088 |

---

## Dashboard

A single-page dashboard served by the API:

- **Live system** — service topology with live health, fault injection, and
  one-click remediation through the real executor. Investigating a *victim*
  service surfaces a banner naming the true cascade origin.
- **Groundhog** — simulation results and on-demand trace replay. Traces are not
  stored; they are **regenerated** from the seed and are byte-identical.
- **Retrieval** — run one query through dense, sparse and hybrid side by side.
  Try `exit code 137 container killed`: dense returns an unrelated postmortem,
  hybrid returns the correct OOMKilled runbook.

---

## Repository

```
victim/          3 FastAPI services, one image, 6 faults, 4 admin endpoints
pipeline/        Kafka producer, Flink SQL job, load harness
rag/             corpus, BM25 sparse vectors, HNSW index, hybrid retrieval, benchmark
agents/          LangGraph graph: triage, investigator, runbook, planner, postmortem
remediation/     ports, action registry, idempotency ledger, safe executor
groundhog/       virtual clock, seeded RNG, lossy network, invariants, seed sweep
api/             FastAPI read model + demo control
frontend/        single-page dashboard
observability/   Prometheus config, Fluent Bit, provisioned Grafana dashboard
deploy/k8s/      Kubernetes manifests (kustomize)
docs/            ROADMAP · BUGS · PROJECT_PLAYBOOK · benchmarks/
```

---

## Design decisions

**Flink SQL, not PyFlink** — built on arm64, where `apache-flink`'s Python
wheels are unreliable. The plain Java image is multi-arch and the job needs no
Python runtime at all.

**Kafka in KRaft mode** — one less container and ~300 MB saved on a memory
budget where that is 8%. Zookeeper is also deprecated for Kafka.

**phi3:mini, not Mistral-7B** — measured. Mistral averaged **166 s** per triage
call against phi3's **6.3 s**; 4.4 GB of weights plus a 3.8 GB VM exceeds 8 GB
and swaps, so that is swap rate, not compute rate. Over a 200-run evaluation
that is 46 hours versus 1.7. phi3 is also weak at structured tool calling, which
is *why* the investigator calls its probes directly in Python and uses the model
only to summarise.

**RRF, not weighted score fusion** — cosine is bounded, BM25 is unbounded and
corpus-dependent. Summing them needs a constant that must be retuned whenever
the corpus changes. RRF fuses on rank and needs no tuning.

**Ports as Protocols** — the executor imports no `time`, no `random`, no HTTP
client. If even one source of nondeterminism stayed hardcoded, no failing seed
would reproduce and Groundhog would be worthless.

**Risk declared in the registry, never by the model** — asking an LLM whether
its own action is dangerous puts that judgement in the worst possible place.

---

## What is not built

- **Fine-tuning.** `finetuning/` holds a working scraper and 128 processed
  postmortems. No training script, no adapter. 128 examples would not produce a
  usable one.
- **Concurrent-incident simulation.** Groundhog is single-threaded. It models
  network faults and crash/recovery, not two incidents racing on one service.
  That is the next invariant worth testing.
- **A live cloud deployment.** The Kubernetes manifests validate with
  `kubectl kustomize` but have not been applied to a real cluster.
- **A walkthrough demo.** The launch video above is a motion-graphics piece,
  not a screen recording of the system running. A real walkthrough — inject a
  fault, watch the cascade banner name the origin, remediate — is still to do.
- **The harder RCAEval suites.** [docs/BENCHMARK.md](docs/BENCHMARK.md)
  covers RE1 only — 2 of 9 datasets. Train Ticket (40+ services) and the
  multi-source RE2/RE3 suites are not run, and all three rankers should be
  expected to fall there.
- **Live production telemetry.** The retrieval corpus is 98% real (129 public
  postmortems from 79 organisations) and the RCAEval evaluation uses somebody
  else's data, but PagedOut's own services and generated telemetry are
  synthetic.

---

## Author

**Nikhil Gade** · [LinkedIn](https://linkedin.com/in/nikhil--gade) · [Email](mailto:nikhilgade.me@gmail.com)
