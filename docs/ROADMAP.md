# PagedOut — Roadmap

**PagedOut** — Autonomous Incident Remediation, Validated by Deterministic Simulation Testing

This replaces the original 8-phase plan. The shape of the project changed once it
became clear what the interesting problem actually was.

---

## What changed and why

The original plan was a breadth checklist: streaming, RAG, agents, fine-tuning,
Slack, multi-tenant. Finishing all of it would have produced a complete but
unremarkable project in a saturated category — "LLM agent for incident response"
is the most common portfolio project of 2026, and there are funded companies
doing exactly it.

The pivot: **PagedOut takes destructive actions on infrastructure.** The dangerous
failures are not "the model picked a bad action" — they are concurrency and
partial failure. A dropped ack causes a retry causes a double restart. A
checkpoint recovers and replays an action that already ran. A rollback interleaves
with an in-flight remediation.

Those bugs need one specific interleaving out of millions. A 200-run integration
suite will never find them. **Deterministic simulation testing will.**

So the project becomes two halves:

```
PagedOut    the system   — Kafka -> Flink -> agents -> remediation
Groundhog   the prover   — deterministic fault simulator that breaks it
```

The narrative is "I built it, then I built the thing that breaks it." That arc is
worth more than either half alone.

### Language decision

**Python throughout, for now.** The hard part of DST is not the language — it is
the discipline of routing every source of nondeterminism through an injectable
interface. That skill is language-agnostic, and code you can defend line by line
in an interview beats faster code you cannot explain.

A Rust port is a **stretch goal** (Phase 8), valuable specifically because it
produces a measured before/after rather than an assumed one.

### Cut from the original plan

| Cut | Why |
|---|---|
| Mistral-7B fine-tuning | 128 examples will never produce a usable adapter. Dilutes the story and invites a question with no good answer. |
| Slack bot | Zero engineering signal. |
| Multi-tenant / multi-cluster | Scope inflation with nothing to show for it. |
| Model routing | Marginal, and unmeasurable at this scale. |
| Arize Phoenix / MLflow / LangSmith | Observability theatre. Prometheus and Grafana already cover what matters. |

Cutting these is not a retreat. Ten shallow things read worse than three deep
ones, and every cut item was breadth, not depth.

---

## Status at a glance

| Phase | Name | Status |
|---|---|---|
| 0 | Foundation | ✅ Complete, verified |
| 1 | Ingestion | ✅ Complete, verified |
| 2 | Knowledge base | ✅ Complete, verified |
| 3 | Agents | ⚠️ Written, end-to-end run unverified |
| 4 | Safe execution | ⬜ Not started — **next** |
| 5 | Groundhog core | ⬜ Not started |
| 6 | Bug hunting | ⬜ Not started |
| 7 | Proof | ⬜ Partial (README only) |
| 8 | Stretch | ⬜ Optional |

---

## Phase 0 — Foundation ✅

**Goal:** infrastructure, plus an application that can actually break.

Nothing downstream is measurable without a system that produces real failures.
An agent investigating `random.uniform()` is a print statement.

**Delivered**
- `victim/app.py` — three FastAPI services in a dependency chain
  (checkout → payment → ledger), one image configured by environment
- Six injectable faults, each producing real failures and real metrics
- Four admin endpoints that genuinely repair state, each returning `changed: bool`
- `docker-compose.yml` — Kafka (KRaft), Qdrant, Postgres, Redis, Prometheus,
  Grafana, Fluent Bit, Flink; memory-capped and split into profiles
- `observability/fluent-bit/` — ships victim logs into Kafka, already conforming
  to the incident-signal schema

**Verified**
- Cascade: fault on ledger → ledger 503, payment 502, checkout 502
- Repair: `bad_deploy` 3/10 success → rollback → 10/10
- Idempotency signal: second `pool/drain` reports `changed: false`
- 1,509 log records shipped with full schema
- Victim stack: 531 MB across 8 containers

**Model decision (measured):** phi3:mini at 6.3s per triage call versus
Mistral-7B at 166s. The 7B model is swap-bound on an 8 GB machine, not
compute-bound. A 200-run evaluation would take 46 hours on Mistral, 1.7 on phi3.

---

## Phase 1 — Ingestion ✅

**Goal:** turn a burst of raw signals into one incident, and prove the pipeline's
limits.

**Delivered**
- `pipeline/flink/jobs/incident_pipeline.sql` — normalize three topics onto one
  schema, then a 30s event-time tumbling window grouped by
  (service, incident_type) with a `HAVING` noise gate
- `pipeline/flink/submit.sh` — cancels running jobs before submitting
- `pipeline/log_generator.py` — confluent-kafka producer with backpressure handling
- `pipeline/loadtest.py` — ramp harness, producer in a separate process

**Measured**

| target | produced | consumed | flink_out | delivery | p99 |
|---|---|---|---|---|---|
| 16,000 | 15,999 | 15,999 | 320,004 | 100% | 80 ms |
| **32,000** | **31,986** | **31,986** | 639,720 | **100%** | **68 ms** |
| 64,000 | 63,994 | 29,751 | — | 46.5% | 1,186 ms |

- Sustained **32,000 events/sec at p99 68ms**, saturating at 64K
- Correlation: **63.5:1** signal-to-incident collapse
- Checkpoints: 14 completed, 0 failed, 138 KB, 89 ms, RocksDB confirmed

---

## Phase 2 — Knowledge base ✅

**Goal:** real corpus, hybrid retrieval, measured against a baseline.

**Delivered**
- `rag/corpus.py` — 1,342 documents (129 postmortems chunked sentence-aligned
  at 800 chars with 150 overlap, plus 20 runbooks kept whole)
- `rag/bm25.py` — BM25 as sparse vectors, full term weight on the document side
  so a sparse dot product computes the BM25 score
- `rag/index.py` — HNSW (m=32, ef_construct=200) plus an exact-scan baseline
  collection built purely for comparison
- `rag/retrieve.py` — dense / sparse / hybrid with RRF fusion
- `rag/benchmark.py` — 96 queries × 6 repeats across three configurations

**Measured**

| | baseline (exact) | hybrid (HNSW + BM25 RRF) |
|---|---|---|
| Recall@3 | 67.7% | **90.6%** |
| MRR | 0.609 | **0.813** |
| search p50 | 3.67 ms | 5.14 ms |
| embed p50 | 9.69 ms | 9.90 ms |

**Honest finding:** hybrid buys accuracy, not speed. At 1,342 documents the
embedding forward pass dominates and exact scan is already fast. There is no
latency win to claim.

---

## Phase 3 — Agents ⚠️

**Goal:** triage, investigate with real telemetry, plan with a validated schema.

**Delivered, verified**
- `agents/tools.py` — every probe hits something real: Prometheus instant
  queries, `/health` endpoints, declared dependency topology. Correctly
  separates origin from cascade victim.

**Delivered, unverified**
- `agents/planner_agent.py` — closed action vocabulary, Pydantic validation,
  retry with the validation error fed back, deterministic fallback
- `agents/graph.py` — planner node added, compiled with a Postgres checkpointer
- `agents/runbook_agent.py` — rewired to the Phase 2 hybrid retriever
- `agents/run_pipeline.py` — `--live` mode drives a real fault end to end

**Definition of done**
- [ ] One full `--live` run completes: fault → Flink correlation → agent graph → plan
- [ ] Postgres checkpoints written and listable
- [ ] Planner produces a schema-valid plan, or falls back deterministically
- [ ] Measure triage accuracy across the 6 fault types

**Note:** requires Docker + Ollama up. Fold into the next session that needs the
stack, rather than spending a heat cycle on it alone.

---

## Phase 4 — Safe execution ⬜ NEXT

**Goal:** make remediation real, and make it correct by construction.

This is the system under test. Groundhog has nothing to break until execution
exists — today `execute_action()` is a print statement, so a simulator would run
millions of scenarios against a no-op.

**Critical constraint: build determinism-clean from line one.** Every clock read,
every random draw, every network call goes through an injectable port. Retrofitting
this later is the refactor that kills DST projects.

**Deliverables**

```
remediation/
  ports.py          Clock, Rng, Transport, Store protocols  <- the seam Groundhog uses
  registry.py       action allowlist; per-action parameter schema
  idempotency.py    key derivation + dedup store; at-most-once semantics
  executor.py       dry-run -> execute -> verify -> rollback; retry with backoff
  rollback.py       post-action health check, revert on regression
  approval.py       gate for high-risk actions
```

**Design notes**
- **Idempotency key** derives from (incident_id, action, target, parameters) —
  stable across retries, distinct across genuinely different actions
- **Dry-run** simulates the action and checks the predicted result before the
  real call; a dry-run that predicts no change skips execution entirely
- **Rollback** compares service health before and after; regression triggers revert
- **Retry** uses bounded exponential backoff with a deadline, never unbounded

**Definition of done**
- [ ] Action executes at-most-once under arbitrary retry
- [ ] Every real action is preceded by a dry-run
- [ ] Rollback fires and restores health when an action makes things worse
- [ ] High-risk actions block on approval and never auto-execute
- [ ] Executor has zero direct calls to `time`, `random`, or any socket
- [ ] Integration test: 200 runs against the live victim app, 0 invalid executions,
      0 duplicate actions

That last line restores the two resume bullets that were lost.

---

## Phase 5 — Groundhog core ⬜

**Goal:** a deterministic simulator in which every failure reproduces exactly
from a 64-bit seed.

**Deliverables**

```
groundhog/
  clock.py        virtual clock; logical time only, never wall clock
  rng.py          seeded PRNG; the single source of all nondeterminism
  scheduler.py    single-threaded event loop, priority queue on logical time
  network.py      fault injection: drop, delay, reorder, duplicate, partition
  world.py        simulated services, store, and failure modes
  trace.py        event log + serialization for replay and diffing
  runner.py       run(seed) -> Trace
```

**The one rule:** nothing in the simulated system may observe anything outside
the simulation. No `time.time()`, no `random`, no `os.urandom`, no sockets, no
threads, no set iteration where order matters. Determinism is binary — it holds
or the project has failed.

**Definition of done**
- [ ] `run(seed=N)` produces a byte-identical trace across 1,000 consecutive runs
- [ ] Byte-identical across process restarts and across machines
- [ ] Network faults are reproducible: same seed, same packets dropped
- [ ] Crash injection at arbitrary points, with recovery from the durable store
- [ ] Time compression measured: simulated seconds per wall-clock second

**Build the determinism proof first**, before any fault modelling. If seeded
replay does not hold on a toy state machine, nothing downstream matters.

---

## Phase 6 — Bug hunting ⬜

**Goal:** find real safety violations, minimize them, and prove they reproduce
outside the simulator.

**Deliverables**

```
groundhog/
  invariants.py   safety properties, checked after every simulation step
  explore.py      seed sweeping, parallel across cores
  shrink.py       trace minimization (delta debugging)
  report.py       findings with reproducing seeds
```

**Invariants to assert**
- no action executes twice for the same idempotency key
- no action executes after a rollback for that incident
- crash and recover never duplicates a side effect
- concurrent incidents on one service never interleave destructively
- the system always converges to healthy or escalates, never silently stalls
- an approval-gated action never executes without approval

**Definition of done**
- [ ] ≥1 genuine safety violation found, with a reproducing seed
- [ ] Each failing trace shrunk to a minimal reproduction
- [ ] Each simulator-found bug **reproduced against the live Docker stack**
- [ ] Each bug fixed, and the seed added to a regression suite
- [ ] Measured: interleavings explored, wall clock, time compression factor
- [ ] Measured: how many of these the 200-run integration suite found (expect 0)

**That last measurement is the headline of the entire project.**

---

## Phase 7 — Proof ⬜

**Goal:** make it legible in under five minutes.

- [ ] README rewritten for the combined narrative
- [ ] Architecture diagram covering both halves
- [ ] Results tables: throughput, retrieval, simulation, bugs found
- [ ] `docs/BUGS.md` — each finding with seed, minimal trace, root cause, fix
- [ ] Grafana dashboard JSON, provisioned
- [ ] 2–3 minute demo: inject fault → correlate → plan → remediate → then show
      Groundhog finding a bug the integration suite missed
- [ ] `PROJECT_PLAYBOOK.md` updated

---

## Phase 8 — Stretch ⬜

Only after Phases 4–7 are complete and Groundhog has found real bugs.

- [ ] **Rust port of the simulation core.** Value is the *measured* before/after,
      not the language. Only worth doing if you learn enough Rust to defend it.
- [ ] **TLA+ specification** of the remediation protocol, model-checked for
      at-most-once execution under arbitrary crash and partition schedules.
      Highest signal-per-hour of anything remaining.
- [ ] **Cascade-depth experiment.** Extend the victim chain to 6+ services and
      measure how root-cause attribution accuracy decays with distance from the
      alarm. Produces a chart nobody currently has.

---

## Build order

```
1. Phase 4 executor            cold, Postgres only
2. Phase 5 determinism proof   cold, zero containers
3. Phase 5 simulator           cold
4. Phase 6 invariants + hunt   cold
5. Phase 3 verification   ┐
6. Phase 6 real repro     ├─ one hot session, stack up
7. Phase 7 demo capture   ┘
```

Most of the remaining work needs no containers at all. That is the nature of
simulation, and it is a genuine advantage on a memory-constrained laptop.

---

## What each phase makes honest

| Phase | Claim it earns |
|---|---|
| 1 | 32K events/sec at 68ms p99; 63.5:1 correlation |
| 2 | 1,342 documents; Recall@3 67.7% → 90.6% |
| 3 | severity classification and LLM planning over retrieved context |
| 4 | 0 invalid executions, 0 duplicate actions across 200 runs |
| 6 | N interleavings explored, K safety violations the test suite missed |
| 8 | Rust speedup; formally verified at-most-once execution |

Nothing goes on a resume before the phase that earns it is done and measured.
