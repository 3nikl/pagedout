# PagedOut — Project Playbook

**Purpose:** everything you need to answer any question about this project in an interview.
Written to be read start to finish once, then used as a reference.

**Last updated:** 2026-09-24, end of the Phase 1 build session.
**Build machine:** Apple Silicon (arm64), 8 CPU cores, 8 GB RAM, Docker Desktop VM capped at 3.8 GB.
That memory number drives a surprising number of the decisions below — remember it.

---

## Table of contents

1. [How to use this document](#1-how-to-use-this-document)
2. [The plan](#2-the-plan)
3. [Architecture](#3-architecture)
4. [Tech stack — every choice and why](#4-tech-stack--every-choice-and-why)
5. [Build log: every file, what it does, why it is written that way](#5-build-log)
6. [Every bug we hit](#6-every-bug-we-hit-the-most-valuable-section)
7. [Decisions and trade-offs](#7-decisions-and-trade-offs)
8. [Your resume: what you can defend and what you cannot](#8-your-resume-what-you-can-defend-and-what-you-cannot)
9. [Interview question bank](#9-interview-question-bank)
10. [What is not built yet](#10-what-is-not-built-yet)

---

## 1. How to use this document

Three rules for the interview:

1. **Never quote a number you did not measure.** Every number in this doc has a
   measurement behind it. If a number is not in this doc, you did not measure it.
2. **"Not built yet" is a complete, acceptable answer.** Engineers respect a clear
   boundary. They do not respect a vague claim that collapses on the second question.
3. **The bugs are your best material.** Section 6 is the most valuable part of this
   document. Anyone can wire a tutorial together; the person who can explain why
   `json.ignore-parse-errors` silently broke their window operator has actually built
   something.

---

## 2. The plan

The project is built in phases. Each phase depends on the one before it, and each one
exists to make a specific claim measurable.

| Phase | Goal | Why it must come in this order |
|---|---|---|
| **0. Foundation** | Infra + a real app that can break | You cannot measure incident response without incidents |
| **1. Ingestion** | Kafka → Flink → correlated incidents | You cannot claim throughput without a pipeline to load-test |
| **2. Knowledge base** | Real corpus + hybrid retrieval | You cannot claim retrieval latency without a real index |
| **3. Agents** | Triage → investigate → plan | You cannot execute a plan you have not generated |
| **4. Safe execution** | Validate, dry-run, idempotency, rollback | You cannot count "invalid executions" without validation |
| **5. Evaluation** | Chaos + 200-run harness | This is where the safety numbers come from |
| **6. Proof** | README, diagram, dashboard, demo | What a recruiter actually sees |
| **7. Advanced** | Slack, fine-tuning, tracing | Optional, not on the resume |

**Current position: Phase 0 complete, Phase 1 at 4 of 6 items.**

The ordering principle worth saying out loud in an interview:

> "Every phase exists to make the next phase's number honest. I built the victim app
> before the agents because an agent that investigates `random.uniform()` is not an
> agent, it is a print statement. I built the execution layer before the evaluation
> harness because you cannot count invalid executions if nothing executes."

---

## 3. Architecture

### 3.1 The system as built today

```
┌──────────────────────────────────────────────────────────────────────────┐
│  VICTIM APPLICATION  (3 FastAPI services, one image, env-configured)     │
│                                                                          │
│    checkout-service ──HTTP──► payment-service ──HTTP──► ledger-service   │
│       :8081                      :8082                     :8083         │
│                                                                          │
│    each exposes:  /api/process   business call, calls downstream         │
│                   /health        liveness + fault state                  │
│                   /metrics       Prometheus exposition                   │
│                   /chaos/{fault} inject one of 6 faults                  │
│                   /admin/*       4 real remediation endpoints            │
└───────────────┬──────────────────────────────────┬───────────────────────┘
                │ scrape /metrics every 5s         │ JSON logs to stdout
                ▼                                  ▼
        ┌───────────────┐                  (log shipper: NOT BUILT YET)
        │  Prometheus   │                          │
        │    :9090      │                          │
        └───────┬───────┘                          │
                │                                  │
                ▼                                  │
        ┌───────────────┐                          │
        │   Grafana     │                          │
        │    :3000      │                          │
        └───────────────┘                          │
                                                   │
┌──────────────────────────────────────────────────┼───────────────────────┐
│  INGESTION                                       ▼                       │
│                                                                          │
│  log_generator.py ──► Kafka (KRaft, 4 partitions/topic)                  │
│   (synthetic)          ├── logs.raw                                      │
│                        ├── metrics.raw                                   │
│                        └── alerts.raw                                    │
│                                 │                                        │
│                                 ▼                                        │
│                        ┌────────────────────────────────┐                │
│                        │  Apache Flink 1.20.5           │                │
│                        │  1 JobManager, 1 TaskManager   │                │
│                        │  4 slots, RocksDB state        │                │
│                        │  checkpoints every 10s         │                │
│                        │                                │                │
│                        │  Job: pagedout-incident-       │                │
│                        │       pipeline (STATEMENT SET) │                │
│                        │                                │                │
│                        │  1. normalize: 3 topics ──►    │                │
│                        │     one incident-signal schema │                │
│                        │  2. correlate: 30s tumbling    │                │
│                        │     window, GROUP BY           │                │
│                        │     (service, incident_type)   │                │
│                        └────────┬───────────────────────┘                │
│                                 │                                        │
│                     ┌───────────┴────────────┐                           │
│                     ▼                        ▼                           │
│            signals.normalized      incidents.correlated                  │
│            (every event, one       (ONE row per real                     │
│             common schema)          incident burst)                      │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼  ← Phase 3 will consume from here
┌──────────────────────────────────────────────────────────────────────────┐
│  AGENT LAYER  (LangGraph — currently still fed by hardcoded test data)   │
│                                                                          │
│    [Triage] ──confidence<0.5──► [Escalate] ──► END                       │
│        │                                                                 │
│        ├─0.5–0.7─► [Runbook] ──┐                                         │
│        └─>0.7────► [Investigator]                                        │
│                          │                                               │
│                          ▼                                               │
│                     [Runbook] ◄──── Qdrant vector search                 │
│                          │                                               │
│                          ▼                                               │
│                     [Remediate]  ← execution still simulated             │
│                          │                                               │
│                          ▼                                               │
│                    [Postmortem] ──► END                                  │
│                                                                          │
│    LLM: phi3:mini via Ollama on the HOST (not containerised)             │
└──────────────────────────────────────────────────────────────────────────┘

Supporting: PostgreSQL 16 (for Phase 3 checkpointing), Redis 7.2, Qdrant v1.12.4
```

### 3.2 The correlation flow — the single most important diagram

This is what Phase 1 actually achieves. Learn this cold.

```
ONE real incident on payment-service (database connection exhaustion)
fans out into many signals:

    logs.raw      ████████████████████████  48 log lines
    metrics.raw   ██████                    12 metric samples
    alerts.raw    ██████                    12 alerts
                  ─────────────────────────────────────────
                  72 separate Kafka messages

Without correlation, that pages the on-call engineer 72 times.

                          │
                          ▼
        ┌─────────────────────────────────────────┐
        │  Flink: TUMBLE(30s) over event_time     │
        │  GROUP BY window, service, incident_type│
        │  HAVING alert_count >= 1                │
        │      OR log_count   >= 5                │
        └─────────────────────────────────────────┘
                          │
                          ▼
    incidents.correlated:  1 row
      service=payment-service
      incident_type=database_connection_exhaustion
      severity=P1            ← MIN(severity), 'P1' < 'P2' lexically
      signal_count=72
      log_count=48  metric_count=12  alert_count=12
      distinct_pods=12

MEASURED RESULT (verified 2026-09-24):
    3,684 raw signals  →  58 incidents  across 3 windows
    collapse ratio 63.5 : 1
    every emitted incident satisfied the HAVING rule
```

### 3.3 Request flow through the victim app

```
GET /api/process on checkout-service
  │
  ├─ apply_request_faults()          ← latency_spike sleeps, error_burst raises,
  │                                     memory_leak allocates 4MB and may 503
  │
  ├─ async with checkout_connection()  ← borrows from the pool
  │     │                                raises 503 if pool_in_use >= pool_size
  │     │
  │     ├─ write to local cache
  │     │
  │     └─ httpx GET payment-service/api/process
  │            │
  │            └─ resp.raise_for_status()   ← CRITICAL: httpx does NOT raise on
  │                                            5xx by itself. Without this the
  │                                            cascade is invisible.
  │            └─ on failure → HTTP 502 upstream
  │
  └─ finally: record latency histogram, refresh gauges
```

---

## 4. Tech stack — every choice and why

For each tool: what it is, why we chose it, what we rejected, and the honest trade-off.

### 4.1 Apache Kafka 3.8.1 (KRaft mode)

**What:** distributed event log. Three input topics, two output topics, 4 partitions each.

**Why Kafka:** we need replayable, partitioned, durable event streams that Flink can
consume with offset tracking. A queue like RabbitMQ deletes on consume; we need replay
for reprocessing and for load tests.

**Why KRaft and not Zookeeper:** KRaft is Kafka's built-in consensus protocol (KIP-500),
GA since 3.3. It removes the Zookeeper dependency entirely.
- Saves one container and roughly 300 MB of RAM — on a 3.8 GB budget that is 8%.
- Zookeeper is deprecated for Kafka and removed in 4.0, so this is the forward path.
- Simpler failure model: one system to reason about instead of two.

**Config worth knowing:**
- `KAFKA_HEAP_OPTS: -Xmx512m -Xms256m` — default heap is 1 GB, too much here.
- Two listeners: `PLAINTEXT` on `kafka:9092` for in-network clients (Flink),
  `PLAINTEXT_HOST` advertised as `localhost:9092` for host clients (the generator).
  **This dual-listener pattern is a classic interview question** — a broker advertises
  the address clients should reconnect to, and that address differs depending on whether
  the client is inside the Docker network or on the host.
- `KAFKA_NUM_PARTITIONS: 4` — matches the Flink slot count so parallelism can scale.

### 4.2 Apache Flink 1.20.5

**What:** distributed stream processor. Runs our normalize + correlate job.

**Why Flink and not Kafka Streams or Spark Structured Streaming:**
- **True event-time processing with watermarks.** Incidents arrive out of order; we need
  windows keyed on when the event *happened*, not when it arrived.
- **Real windowing primitives.** `TUMBLE` as a table-valued function is a first-class
  construct.
- **Managed keyed state with RocksDB**, so window state can exceed memory.
- **Exactly-once checkpointing.** Spark Structured Streaming is micro-batch; latency
  floors out at the batch interval. Kafka Streams is a library, not a cluster, so no
  independent scaling or a job UI.

**Why Flink SQL and not PyFlink — an important decision:**
The whole job is SQL. We deliberately did **not** use PyFlink.
- We build on **arm64**, where `apache-flink`'s Python wheels are unreliable.
- The plain `flink:1.20-java17` image is multi-arch and needs no Python runtime.
- Flink SQL compiles to the same DataStream operators; there is no capability loss for
  this workload, and no Python↔JVM serialisation overhead.

**Trade-off to admit:** SQL is harder to unit-test than PyFlink, and complex custom
logic (a stateful de-duplicator, say) would want the DataStream API. For normalize +
window + aggregate, SQL is strictly better.

**Config worth knowing:**
- `state.backend.type: rocksdb` — spills to disk, supports incremental checkpoints.
  The alternative (`hashmap`) is faster but bounded by heap.
- `execution.checkpointing.interval: 10s`, mode `EXACTLY_ONCE`.
- `table.exec.source.idle-timeout: 5s` — stops an idle partition from freezing the
  watermark. With 4 partitions and few services, some partitions get no traffic; without
  this the watermark never advances and **no window ever closes.**

### 4.3 Qdrant v1.12.4

**What:** vector database for runbook retrieval.

**Why Qdrant over pgvector / FAISS / Pinecone:**
- Native **payload filtering** combined with vector search — we filter by `incident_type`
  *during* the search, not after, so we do not lose recall.
- HNSW by default; purpose-built rather than an extension.
- FAISS is a library with no server, no payloads, no filtering. Pinecone is hosted and
  costs money; this project must run free and offline.
- We do run PostgreSQL, so pgvector was available — but pgvector's filtering story is
  weaker and we wanted the index decoupled from the transactional store.

**Honest current state:** we use dense-only search with a payload filter. **No BM25, no
hybrid, no reranking yet.** That is Phase 2.

### 4.4 Ollama + phi3:mini

**What:** local LLM server exposing an OpenAI-ish HTTP API on `:11434`.

**Why not containerised:** on macOS, a Linux container cannot access Metal. Ollama on the
host gets GPU acceleration; in a container it would be CPU-only and several times slower.
This is a deliberate, defensible exception to "everything in Compose."

**Why phi3:mini and not Mistral-7B — this is measured, not assumed:**

Benchmark on this machine with the stack running, realistic triage prompt, 3 runs each:

| Model | Size | Median latency | Throughput |
|---|---|---|---|
| `phi3:mini` | 2.2 GB | **6.3 s** | 8.6 tok/s |
| `mistral:latest` | 4.4 GB | **166 s** | 0.4 tok/s |

**26× slower.** The cause is memory, not compute: 4.4 GB of weights plus a 3.8 GB Docker
VM exceeds 8 GB of physical RAM, so the model pages to disk. 0.4 tok/s is swap rate.

Consequence for the 200-run evaluation in Phase 5:
- phi3:mini ≈ **1.7 hours**
- Mistral-7B ≈ **46 hours**

**This is a genuinely good interview story.** You had a hypothesis, you measured, the
measurement overturned the plan, you changed the plan and can quantify the cost.

**Secondary reason phi3:mini shapes the design:** it is unreliable at structured tool
calling. That is *why* `investigator_agent` calls its probes directly in Python and uses
the LLM only to summarise. That is an architecture decision forced by a model constraint.

### 4.5 LangGraph 1.1.6

**What:** builds the agent pipeline as a directed graph with typed shared state.

**Why LangGraph and not a plain LangChain chain:**
Triage must divert low-confidence incidents to a human *without running the rest of the
pipeline.* That is a conditional edge over shared state. A linear chain cannot express
branching; you would end up hand-rolling if/else around chain invocations and lose the
state model, the checkpointing hooks and the graph visualisation.

**Version note — this matters:** `requirements.txt` originally pinned `langgraph==0.2.28`
and `langchain==0.2.16`, but the code was written against the **1.x** APIs actually
installed (`graph.compile()`, `query_points()`). A fresh clone could not run. We repinned
to the verified-working set. Lesson: pin to what you actually tested.

### 4.6 FastAPI + Prometheus client (victim app)

**Why FastAPI:** async-native, which matters because the victim app must hold connections
open and make downstream HTTP calls concurrently. Pydantic request models give free
validation on the admin endpoints. Auto-generated OpenAPI docs.

**Why `prometheus_client`:** the standard Python exposition library. We use:
- `Counter` for monotonic totals (`pagedout_requests_total`)
- `Histogram` for latency (gives you quantiles via `histogram_quantile`)
- `Gauge` for values that go up and down (pool usage, heap percent)

**Why one image for three services:** identical code, different env vars
(`SERVICE_NAME`, `DOWNSTREAM_URL`). This is how real microservice demos are built — one
artifact, config-driven. Fewer things to keep in sync.

### 4.7 confluent-kafka (not kafka-python)

**Why we switched:** the original generator used `kafka-python`, a pure-Python client.
For a load test that is disqualifying: at high rates the *generator* becomes the
bottleneck and you end up benchmarking Python, not Flink. `confluent-kafka` wraps
`librdkafka` (C) and sustains an order of magnitude more throughput.

**Producer tuning in the code and why:**
- `linger.ms: 5` — wait 5 ms to batch. Trades a little latency for much better throughput.
- `batch.size: 262144` (256 KB) — bigger batches, fewer syscalls.
- `compression.type: lz4` — fast compression, cuts network and disk.
- `acks: 1` — leader acknowledgement only. `acks=all` is safer but slower; for synthetic
  load generation, leader-ack is the right trade.
- `queue.buffering.max.messages: 1_000_000` — deep local queue to absorb bursts.

### 4.8 Docker Compose with profiles

**Why profiles:** the full stack does not fit a 3.8 GB VM. Profiles let one file describe
the whole system while bringing up only what a phase needs.

```bash
docker compose up -d                    # core: Kafka, Qdrant, Postgres, Redis
docker compose --profile victim up -d   # + 3 services + Prometheus + Grafana
docker compose --profile flink  up -d   # + Flink cluster
docker compose --profile all    up -d   # everything (needs a bigger VM)
```

**Other Compose techniques used, worth being able to explain:**
- **YAML anchors** (`x-victim-base: &victim-base` + `<<: *victim-base`) — the three victim
  services share one definition. DRY without a templating tool.
- **Healthchecks + `condition: service_healthy`** — ordered startup. `depends_on` alone
  only waits for *start*, not *readiness*, which is a very common production bug.
- **`condition: service_completed_successfully`** — for one-shot init containers.
- **`mem_limit`** on every service — a cap, not a reservation. Measured actual usage for
  the victim stack: **531 MB**.

---

## 5. Build log

Every file, what is in it, and why it is written that way.

### 5.1 `victim/app.py` — 414 lines

The most substantial hand-written file. A microservice you can break on purpose.

**`class JsonLogFormatter(logging.Formatter)`**
Subclasses the stdlib formatter and overrides `format()` to emit one JSON object per
line instead of text.
- *Why a class?* `logging.Formatter` is an extension point — the logging framework calls
  `format(record)` on whatever object you install. Subclassing is the idiomatic hook.
- *Why JSON?* A downstream log shipper can `json.loads()` each line. Parsing unstructured
  log text with regex is how you get 3am pager duty.
- Reads `record.extra_fields` if present, so callers can attach structured context.

**`def emit(level, message, **fields)`**
Thin wrapper over `log.log()` that packs `**fields` into `extra={"extra_fields": ...}`.
- *Why `**kwargs`?* Call sites become `emit("ERROR", "pool exhausted", pool_in_use=20)`
  — structured context with no ceremony.

**`class ServiceState`**
Holds everything mutable: `pool_size`, `pool_in_use`, `version`, `previous_version`,
`cache`, `ballast`, `faults`, `lock`.
- *Why a class and not module globals?* One object to snapshot, one object to reset
  between evaluation runs, and it keeps the remediation surface explicit — if it is on
  `ServiceState`, an action can change it.
- **`@property heap_pct`** — computed from `len(self.ballast)`, not stored. A derived
  value should never be a second source of truth that can drift.
- **`self.lock = asyncio.Lock()`** — the pool counter is read-modify-written from
  concurrent coroutines. Without the lock you get a lost-update race and the pool count
  drifts. *This is a real concurrency answer, not decoration.*
- **`snapshot()`** returns a plain dict for the `/health` response.

**`async def apply_request_faults(endpoint)`**
One function, a sequence of `if fault in state.faults` checks.
- *Why sequential ifs and not a dispatch dict?* Faults **compose** — `latency_spike` plus
  `error_burst` should both apply to the same request. A dict dispatch would pick one.
- `memory_leak` appends a `bytearray(4 * 1024 * 1024)` per request. Real allocation, so
  the heap gauge genuinely climbs; it is not a fake number.
- `error_burst` uses `random.random() < 0.45` — probabilistic, because real error bursts
  are not every request.

**`@asynccontextmanager async def checkout_connection()`**
Borrow/return a pooled connection.
- *Why a context manager?* The `finally` block guarantees the connection is returned even
  if the body raises. This is exactly the resource-safety problem context managers exist
  for. Written as a plain function you would leak a slot on every error path.
- Checks `pool_in_use >= pool_size` under the lock and raises 503 when saturated.

**`async def _background_pressure()`**
An infinite `while True` loop with `await asyncio.sleep(1.0)`, started as a task in
`lifespan`.
- *Why a background task?* `pool_exhaustion` should be reproducible from a *single*
  request. Without background pressure the pool only saturates under concurrent load,
  making evaluation runs non-deterministic.
- *Why `asyncio.create_task` in `lifespan` and not a thread?* Everything else is async;
  a thread would need locking across the sync/async boundary.
- Cancelled on shutdown so the app exits cleanly.

**`@asynccontextmanager async def lifespan(app)`**
FastAPI's modern startup/shutdown hook (replaces the deprecated `@app.on_event`).
Everything before `yield` is startup, after is shutdown.

**Endpoints**
- `GET /api/process` — the business call. `try/except HTTPException/finally`: the
  `finally` always records the latency histogram, so failed requests appear in latency
  data too. Omitting failures from latency metrics is a classic way to lie to yourself.
- `POST /chaos/{fault}` — validates against `VALID_FAULTS`, a `set` (O(1) membership).
- `POST /admin/*` — the four remediation targets. **Every one returns `changed: bool`.**
  That field is the whole point: Phase 5 verifies idempotency by asserting a repeated
  action reports `changed: false`.

### 5.2 `victim/Dockerfile`

Standard layer-caching pattern: copy `requirements.txt`, `pip install`, *then* copy source.
Editing `app.py` does not invalidate the dependency layer, so rebuilds take seconds.

### 5.3 `docker-compose.yml` — 295 lines

Covered in §4.8. Notable additions during the build:

**`kafka-init`** — one-shot container looping over topic names, calling
`kafka-topics.sh --create --if-not-exists`. Exists because Flink's Kafka enumerator lists
partitions up front and fails if the topic is absent (see §6.5).

**`flink-init`** — one-shot `alpine` that `chown -R 9999:9999` the checkpoint volumes.
Exists because named volumes are created root-owned and Flink drops to uid 9999 (see §6.3).

### 5.4 `observability/prometheus.yml`

5-second scrape of the three victim services.

**`relabel_configs`** rewrites `__address__` (`checkout-service:8000`) into a clean
`service` label (`checkout-service`) via regex capture. *Why?* So the label the agents
query matches the service name they already know. Without it every query needs to strip
a port.

### 5.5 `pipeline/flink/Dockerfile`

`FROM flink:1.20-java17`, then `curl` the `flink-sql-connector-kafka` uber-jar into
`/opt/flink/lib/`.
- *Why the **sql**-connector and not `flink-connector-kafka`?* The SQL variant is an
  uber-jar that shades its Kafka client. The plain connector expects you to supply a
  matching `kafka-clients` jar, which is a classpath-conflict trap.
- Also copies the RocksDB state-backend jar from `opt/` to `lib/` so
  `state.backend.type: rocksdb` resolves.

### 5.6 `pipeline/flink/jobs/incident_pipeline.sql` — 212 lines

The heart of Phase 1.

**Structure:**
1. `SET` statements — checkpointing, RocksDB, idle-timeout.
2. Three `CREATE TABLE` sources, one per raw topic.
3. Two `CREATE TABLE` sinks.
4. One `CREATE TEMPORARY VIEW all_signals` — the `UNION ALL`.
5. `EXECUTE STATEMENT SET ... BEGIN ... END` containing both INSERTs.

**`EXECUTE STATEMENT SET` — why:** submits both INSERTs as **one job graph** sharing one
set of task slots. Two separate jobs would each need their own slots and each consume the
source topics independently — double the Kafka read load, double the memory.

**`WATERMARK FOR timestamp AS timestamp - INTERVAL '15' SECOND`**
A watermark is Flink's assertion that "no event older than this will arrive." The 15 s
lag tolerates out-of-order arrival. Too tight and you drop late events; too loose and
windows close slowly.

**`TIMESTAMP_LTZ(3)` not `TIMESTAMP(3)`** — see §6.6. Z-suffixed ISO-8601 is an *absolute
instant*, which is `TIMESTAMP_LTZ`. `TIMESTAMP(3)` is a wall-clock with no zone and its
ISO-8601 parser rejects the `Z`.

**The `UNION ALL` view** projects three different shapes onto one schema. Each branch must
produce identical column types, hence `CAST(NULL AS MAP<STRING, DOUBLE>)` for the log and
alert branches — an untyped `NULL` cannot be unioned against a typed column.

**The correlation query:**
```sql
FROM TABLE(TUMBLE(TABLE all_signals, DESCRIPTOR(event_time), INTERVAL '30' SECOND))
GROUP BY window_start, window_end, service, incident_type
HAVING COUNT(*) FILTER (WHERE signal_type = 'alert') >= 1
    OR COUNT(*) FILTER (WHERE signal_type = 'log')   >= 5
```
- **`TUMBLE` as a TVF** — Flink's modern windowing syntax (replaces the old
  `GROUP BY TUMBLE(...)` form). Non-overlapping fixed 30 s buckets.
- **Grouping key `(service, incident_type)`** — this is the correlation rule. All signals
  about the same failure on the same service land in one group.
- **`COUNT(*) FILTER (WHERE ...)`** — SQL-standard conditional aggregation. One pass over
  the data produces four different counts. Cleaner and faster than four subqueries.
- **`HAVING`** — the noise gate. A single stray ERROR line does not page anyone.
- **`MIN(severity)`** — a deliberate trick. `'P1' < 'P2' < 'P3'` lexically, so `MIN`
  returns the *most severe*. Say this out loud in an interview; it shows you thought
  about the encoding.

### 5.7 `pipeline/flink/submit.sh`

Bash, `set -euo pipefail`.
- Cancels every running job **first** (see §6.7), then submits, then polls the REST API
  until state is `RUNNING`, surfacing the root exception on `FAILED`.
- *Why not just run `sql-client.sh`?* Because doing it by hand is how you end up with two
  jobs fighting over four slots and a mystery `RESTARTING` loop.

### 5.8 `pipeline/log_generator.py` — 390 lines

Rewritten from the original.

**`def iso_now()`**
`datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")`.
- Python's default `isoformat()` gives microseconds and `+00:00`. Flink's ISO-8601 parser
  wants milliseconds and `Z`. This one-liner is the fix for a bug that cost real time.

**`@dataclass(frozen=True) class IncidentTemplate`**
- *Why a dataclass?* Free `__init__`, `__repr__`, `__eq__` for a pure data holder.
- *Why `frozen=True`?* Templates are read-only constants. Freezing makes accidental
  mutation a runtime error and makes the object hashable.
- *Why `field(default_factory=dict)`?* A mutable default argument is shared across all
  instances — the classic Python footgun. `default_factory` builds a fresh dict per
  instance.

**`class IncidentGenerator`**
- Holds the `Producer` and a `sent` counter. State across many calls ⇒ a class.
- **`_send()`** wraps `producer.produce()` in `while True: try/except BufferError`.
  *Why?* librdkafka has a bounded local queue; when full it raises `BufferError`. The
  loop calls `poll(0.1)` to let it drain, then retries. **This is backpressure handling** —
  without it, a load test crashes at high rates. Good thing to be asked about.
- **`emit_incident(fanout=4)`** produces one correlated burst: `fanout` logs + 1 metric +
  1 alert, all sharing `service`, `incident_type`, `ts` and `pod`. This is deliberately
  the fan-out the Flink job must collapse.
- **`_send` stamps `emitted_at_ms`** — epoch millis, for measuring true end-to-end latency
  in the load test.

**`def run(rate, duration, fanout, services)`** — the rate limiter.
- Computes `interval = 1.0 / incidents_per_sec`, tracks `next_emit`, and on each loop
  either emits or sleeps a short slice.
- **`if next_emit < now: next_emit = now + interval`** — if we fall behind, do *not* try
  to catch up in a burst. Catch-up bursts produce a sawtooth load profile and make
  latency numbers meaningless.
- Installs `SIGINT`/`SIGTERM` handlers that set a flag rather than killing mid-produce,
  so `flush()` runs and no messages are silently lost.

### 5.9 Pre-existing files (from before this session)

- `agents/state.py` — `PagedOutState(TypedDict)`. `messages` uses
  `Annotated[list, add_messages]`, LangGraph's reducer that appends rather than replaces.
- `agents/graph.py` — builds the `StateGraph`, adds 6 nodes, one conditional edge.
- `agents/triage_agent.py` — prompts for JSON, parses by finding the outer `{...}` with
  `find('{')` / `rfind('}')`. *Why not `json.loads` directly?* Small models wrap JSON in
  prose. Brace-matching is more robust; falls back to escalation on failure.
- `agents/investigator_agent.py` — **probes still return `random.uniform()`.** Phase 3
  replaces these with real Prometheus queries.
- `agents/remediation_agent.py` — `classify_risk()` is `step.startswith("SAFE:")`.
  **`execute_action()` is a print statement.** Phase 4 replaces this.
- `rag/index_runbooks.py` — 20 base runbooks × (12 services + 7 envs + 1) = **400 points
  over 20 distinct documents.** Phase 2 replaces this with a real corpus.
- `finetuning/` — scraper + processor producing **128 postmortems** from
  `danluu/post-mortems`, split 102/26. **No training script exists.**

---

## 6. Every bug we hit (the most valuable section)

Memorise these. They are the difference between "I followed a tutorial" and "I built this."

### 6.1 The fault that injected but never failed anything

**Symptom:** `POST /chaos/pool_exhaustion` succeeded, `/health` showed the fault active,
but every request still returned 200.

**Root cause:** `_background_pressure` filled the pool to `pool_size - 1` (19/20). The
guard is `pool_in_use >= pool_size`, so `19 >= 20` was false and a slot was always free.

**Fix:** saturate fully — `min(state.pool_size, state.pool_in_use + 5)`.

**Lesson:** an off-by-one in a *test fixture* is more dangerous than one in production
code, because it makes your whole evaluation silently vacuous. Everything would have
"passed" while testing nothing.

### 6.2 The cascade that reported success

**Symptom:** `payment-service` returned 503, but `checkout-service` (which depends on it)
returned **200**.

**Root cause:** `httpx` does not raise on HTTP error status. It only raises on *transport*
errors. `resp.json()` on a 503 body parses fine, so checkout treated the error payload as
a valid downstream response.

**Fix:** `resp.raise_for_status()` before `resp.json()`.

**Lesson:** this is a genuine production-grade bug — a service reporting healthy while its
dependency is down. It is also the single most likely thing an interviewer will find
interesting, because it is subtle, real, and the fix is one line.

### 6.3 Flink could not write checkpoints

**Symptom:** `java.io.IOException: Failed to create directory for shared state`.

**Root cause:** Docker named volumes are created `root:root 755`. The Flink entrypoint
drops privileges to uid 9999 (`flink`). Confusingly, `docker compose exec` gave a root
shell, so a naive check suggested permissions were fine — the *JVM* was not root.

**Fix:** a `flink-init` one-shot container that `chown -R 9999:9999` the volumes, wired in
with `condition: service_completed_successfully`.

**Lesson:** container UID mismatches on mounted volumes are one of the most common Docker
production issues. Also: check the permissions of the *process*, not of your debug shell.

### 6.4 The YAML that ate the quotes

**Symptom:** `flink-init` exited 2 with `sh: syntax error: unexpected "&&"`.

**Root cause:** the command was written with a YAML folded scalar (`>`). Compose then
split the resulting string on whitespace, and the quoting that was supposed to keep
`mkdir ... && chown ...` as a single argument to `sh -c` was lost.

**Fix:** exec-form list — `["sh", "-c", "mkdir ... && chown ..."]`.

**Lesson:** prefer exec form for any command containing shell operators. Shell-form
string parsing in Compose is a footgun.

### 6.5 Flink could not find the topics

**Symptom:** `FlinkRuntimeException: Failed to list subscribed topic partitions`, job
stuck in `RESTARTING`.

**Root cause:** `KAFKA_AUTO_CREATE_TOPICS_ENABLE` was true, but Flink's `KafkaSource`
enumerator *lists partitions* during startup rather than producing/consuming. Listing a
non-existent topic does not trigger auto-create.

**Fix:** a `kafka-init` container that explicitly creates all five topics with 4
partitions before Flink starts.

**Lesson:** auto-create is a convenience, not a contract. Production Kafka almost always
disables it — topics should be declared with intentional partition and replication
counts, not conjured by a typo in a consumer.

### 6.6 The silent NULL that poisoned the window — the best bug

**Symptom:** `signals.normalized` received only 208 of 1,200 events.
`incidents.correlated` received **zero**. The job sat in `RESTARTING`.

**Root cause — two compounding problems:**
1. The generator emitted `2026-09-24T19:45:12.345Z`. The column was declared
   `TIMESTAMP(3)`, whose ISO-8601 parser **rejects the `Z` zone designator**. A
   Z-suffixed instant is `TIMESTAMP_LTZ(3)`.
2. `json.ignore-parse-errors = 'true'` meant the failure was **silent**: instead of
   erroring, Flink set the field to `NULL`.

Downstream, the window operator received rows with a NULL row-time and died with
`RowTime field should not be null`. The actual error was three layers away from the cause.

**Fix:** `TIMESTAMP_LTZ(3)` on all timestamp columns.

**Lesson — say this one in interviews:** error-tolerance settings are a double-edged
sword. `ignore-parse-errors` keeps a pipeline alive through malformed input, but it
converts a loud, local failure into a silent, distant one. The correct production pattern
is a **dead letter queue**: keep the pipeline alive *and* keep the evidence.

### 6.7 Two jobs fighting over four slots

**Symptom:** after resubmitting, jobs kept flapping between RUNNING and RESTARTING. Source
metrics showed 2,400 records read when only 1,200 were produced.

**Root cause:** I submitted the job twice. Both copies were consuming the source topics
and competing for the cluster's 4 task slots.

**Fix:** `submit.sh` now cancels all running jobs before submitting.

**Lesson:** doubled input metrics are a strong signal of a duplicate consumer. And
submission should be a script, not a remembered command.

### 6.8 The watermark that never advanced

**Symptom:** correlation produced 0 incidents even after the pipeline was correct.

**Root cause:** we produced a fixed burst, then stopped, then waited. But **event-time
watermarks only advance when new events arrive.** With the generator stopped, the final
window never closed.

**Fix:** verify with *continuous* production, so the watermark keeps moving.

**Lesson:** this is the classic event-time gotcha, and a very common interview question.
In production you handle it with an idle-source timeout (we set
`table.exec.source.idle-timeout: 5s` for idle *partitions*) and by accepting that the last
window of a finite stream needs either a bounded source or an explicit
max-watermark on shutdown.

### 6.9 Dependency pins that could not run

**Symptom:** found during the README audit, not at runtime.

**Root cause:** `requirements.txt` pinned `langgraph==0.2.28` / `langchain==0.2.16`, but
the code used 1.x APIs. It also omitted `langchain-ollama`, `sentence-transformers`,
`requests` and `bs4` entirely — all imported. A fresh clone could not run the project.

**Fix:** repinned to the verified-working set; moved unused packages to a commented
"not yet used" block.

**Lesson:** pin what you tested. An untested requirements file is worse than none,
because it looks authoritative.

---

## 7. Decisions and trade-offs

A compact table. If asked "why did you choose X", the answer is here.

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Stream processor | Flink | Kafka Streams, Spark SS | Event-time windows, RocksDB state, exactly-once, independent scaling |
| Flink API | SQL | PyFlink | arm64 wheel reliability; no Python↔JVM overhead; same operators |
| Kafka coordination | KRaft | Zookeeper | One less container, ~300 MB saved, Zookeeper is deprecated |
| Kafka client | confluent-kafka | kafka-python | Pure Python becomes the load-test bottleneck |
| Vector DB | Qdrant | pgvector, FAISS, Pinecone | Payload filtering during search; free; offline |
| LLM | phi3:mini | Mistral-7B | **Measured 26× faster**; 7B swaps on 8 GB |
| LLM hosting | Ollama on host | Ollama in container | Metal GPU unavailable inside Linux containers on macOS |
| Agent framework | LangGraph | LangChain chains | Conditional routing over typed shared state |
| Victim app | 1 image, 3 configs | 3 codebases | One artifact, config-driven, nothing to keep in sync |
| Compose layout | Profiles | One flat stack | Full stack exceeds the 3.8 GB VM |
| Tool calling | Direct Python calls | LLM tool calling | phi3:mini is unreliable at structured tool calls |
| JSON parsing | Brace matching | `json.loads` | Small models wrap JSON in prose |

---

## 8. Your resume: what you can defend and what you cannot

Read this section twice. You have already applied with this resume, so the question is no
longer "what should it say" but **"what do you say when they ask."**

### 8.1 The honest position

You cannot un-send the applications. What you *can* control is that you never assert
something false in a live conversation. Three things are true at once:

1. Some claims are now genuinely backed by working, measured code.
2. Some are not built yet — and "in progress, here's where it stands" is a perfectly
   normal thing to say about a personal project.
3. **A number you cannot explain is far more damaging than a feature you have not built.**
   Interviewers probe numbers. "How did you measure that?" is the second question, always.

If a claim is not backed, do not defend it. Say where the project actually is and redirect
to what you did build. That reads as integrity plus self-awareness. Bluffing reads as
neither, and it collapses in about two follow-up questions.

### 8.2 Claim-by-claim status

| Resume claim | Reality today | What to say |
|---|---|---|
| **Kafka streaming ingestion** | ✅ Real. KRaft, 5 topics, 4 partitions, confluent-kafka producer with backpressure handling | Defend fully. Talk about the dual-listener config and the `BufferError` backpressure loop |
| **Flink stream processing** | ✅ Real. Flink 1.20.5, SQL job, normalize + 30s tumbling window correlation | Defend fully. **63.5:1 collapse ratio, measured.** Explain the `HAVING` noise gate |
| **RocksDB state + checkpointing** | ✅ Real. 14 checkpoints completed, 0 failed, 138 KB state, 89 ms each | Defend fully. Explain incremental checkpoints and why RocksDB over hashmap |
| **LangGraph multi-agent system** | ✅ Real. 6 nodes, conditional routing on confidence | Defend the graph and routing. **Be upfront that the "planner" currently reads steps from the retrieved runbook rather than generating them** |
| **Qdrant vector RAG** | ⚠️ Partly. Real vector search with payload filtering — but dense-only, no hybrid | "Vector search with payload filtering is working; BM25 hybrid is the next thing I'm adding" |
| **1,043 documents** | ❌ **False.** 400 points over 20 distinct documents (permutations of 20 runbooks) | **Do not use this number.** If it comes up: "that count was wrong — it's 400 vectors over 20 base runbooks, and I'm replacing them with the 128 real postmortems I scraped" |
| **p95 1.4 s → 180 ms** | ❌ **Never measured.** No baseline, no BM25 prefilter | **Do not defend.** "I haven't benchmarked retrieval yet — that's Phase 2" |
| **800 → 12K events/sec @ 940 ms p99** | ⚠️ Pipeline is real, **number is not measured** | "The pipeline is built and running; I haven't run the load test yet." Do NOT quote 12K |
| **0 invalid executions / 200 runs** | ❌ Nothing executes; no harness exists | "That's the evaluation layer, still to build" |
| **0 duplicate actions / 200 runs** | ❌ No idempotency mechanism | Same |
| **Mistral-7B fine-tuned** | ❌ **No training was ever run.** 128 examples prepared, no script | "I collected and prepared the dataset — 128 postmortems in Alpaca format — but I haven't run the fine-tune" |
| **Guardrails AI** | ❌ In requirements, never imported. Risk check is a string prefix | "Currently it's a prefix convention, not real schema validation. Pydantic validation is Phase 4" |
| **Azure MLOps / Next.js / MLflow / Arize / LangSmith / Celery** | ❌ None implemented | "Those were planned stack items I put in the README ahead of building them. I've since rewritten the README to match reality" |

### 8.3 The Mistral-7B question — turn it into your best answer

Your resume implies Mistral-7B. You are running phi3:mini. **This is your strongest
story**, not a weakness:

> "I planned on Mistral-7B and I benchmarked it on my hardware. On an 8 GB machine with
> the Docker stack running, Mistral averaged 166 seconds per triage call at 0.4 tokens
> per second — that's swap rate, not compute rate; the 4.4 GB of weights plus a 3.8 GB
> VM doesn't fit in 8 GB. phi3:mini did the same call in 6.3 seconds. For the 200-run
> evaluation I have planned, that's 46 hours versus 1.7 hours. So I moved to phi3:mini
> and designed around its weakness — it's unreliable at structured tool calling, which
> is exactly why my investigator agent calls its tools directly in Python and only uses
> the model to summarise the evidence."

That answer demonstrates: benchmarking, hardware reasoning, cost/latency trade-offs,
willingness to kill a plan on evidence, and architecture driven by a measured constraint.
It is much better than "I used Mistral-7B."

### 8.4 If asked directly: "your resume says 12K events/sec — walk me through it"

Do not improvise a number. Say:

> "I need to correct that — I have the pipeline built but I haven't run the load test
> yet, so I can't stand behind that figure. What I can tell you is what's actually
> measured: the correlation stage collapses 3,684 raw signals into 58 incidents, a 63.5
> to 1 ratio, and checkpointing runs at 89 milliseconds for 138 KB of state with zero
> failures. The load harness is the next thing I'm building."

Short, honest, immediately pivots to real numbers. Most interviewers will respect it and
move on. Some will not, and that is a cost of the resume as written — but it is a far
smaller cost than being caught inventing.

### 8.5 Numbers you CAN quote today

Memorise these. They are all measured.

| Metric | Value |
|---|---|
| Correlation collapse ratio | **63.5 : 1** (3,684 signals → 58 incidents) |
| Windows observed | 3 tumbling 30 s windows |
| Checkpoints | **14 completed, 0 failed** |
| Checkpoint state size / duration | **138.5 KB / 89 ms** |
| Flink cluster | 1 JM, 1 TM, 4 slots, v1.20.5 |
| Victim stack memory | **531 MB** across 8 containers |
| phi3:mini triage latency | **6.3 s** median (8.6 tok/s) |
| Mistral-7B triage latency | **166 s** median (0.4 tok/s) |
| Generator sustained rate (verified) | 60 ev/s clean, no drops |
| Fault → cascade | payment 503 → checkout 502, ledger 200 |
| Remediation effect | bad_deploy 3/10 success → rollback → **10/10** |
| Corpus (current, honest) | 400 vectors over **20** distinct runbooks |
| Fine-tuning dataset | **128** examples, 102 train / 26 test |

---

## 9. Interview question bank

**"Walk me through the architecture."**
Use §3.1. Say it in one breath: synthetic and real telemetry land in three Kafka topics;
a Flink SQL job normalises them onto one schema and collapses bursts into single incidents
with a 30-second tumbling window; a LangGraph agent pipeline triages, investigates,
retrieves a runbook from Qdrant and plans remediation; Prometheus and Grafana observe the
victim app the whole time.

**"Why Flink and not Kafka Streams?"** → §4.2.

**"What is a watermark?"**
Flink's assertion that no event older than time T will still arrive. It is what lets
event-time windows decide they are complete. We use a 15-second bounded-out-of-orderness
watermark. Two gotchas we actually hit: idle partitions freeze the watermark (fixed with
`table.exec.source.idle-timeout`), and a stopped source means the final window never
closes (§6.8).

**"Exactly-once — do you really have it?"**
Flink's checkpointing gives exactly-once *state* semantics. End-to-end exactly-once to
Kafka additionally requires the transactional sink (`sink.delivery-guarantee`), which we
have **not** configured — so the sink is effectively at-least-once. Being precise about
this distinction is a strong signal.

**"How does the correlation actually work?"** → §3.2 and §5.6. Mention
`COUNT(*) FILTER (WHERE ...)`, the `HAVING` noise gate and the `MIN(severity)` trick.

**"What was the hardest bug?"** → §6.6, the TIMESTAMP_LTZ / ignore-parse-errors one.
It has a great shape: silent failure, symptom three layers from the cause, and a real
lesson about error tolerance versus observability.

**"How would you scale this?"**
Partition count is the parallelism ceiling — currently 4. Increase partitions, raise Flink
parallelism to match, add TaskManagers. Key by `service` so related signals land on the
same partition. The window state is small (138 KB), so RocksDB has plenty of headroom.
The real bottleneck at scale would be the LLM calls, which is an argument for model
routing: a tiny model for triage, a larger one only for root cause.

**"What would you do differently?"**
Build the victim app first — I originally had agents investigating `random.uniform()`,
which meant nothing downstream could be measured. And write the README last: mine
described a system that did not exist, which is a much worse bug than any of the code
ones.

**"What's not done?"** → §10. Answer it directly and without hedging.

---

## 10. What is not built yet

State this plainly if asked. It is a personal project in progress; that is normal.

**Phase 0 remainder**
- Log shipper from victim stdout into Kafka. Logs are structured JSON but nothing consumes them.

**Phase 1 remainder**
- Load test harness with a ramp profile.
- Any throughput or latency measurement. **No p99 number exists.**

**Phase 2 — not started**
- Real corpus (the 128 postmortems are scraped but not indexed).
- Chunking, BM25 prefilter, hybrid retrieval, reranking.
- Retrieval latency baseline and optimised measurement.

**Phase 3 — not started**
- Investigator still returns `random.uniform()` instead of querying the live Prometheus
  that is now running.
- No planner: the "plan" is read verbatim from the retrieved runbook.
- No graph checkpointing; `graph.compile()` takes no checkpointer, so a crash loses state.

**Phase 4 — not started**
- No action registry, no schema validation, no dry-run, no idempotency keys, no rollback.
- The approval queue is an in-memory list that is printed and discarded.
- `execute_action()` is still a print statement — though the victim app now has four real
  admin endpoints waiting for it.

**Phase 5 — not started**
- No chaos scripts, no 200-run harness, no safety measurements.

**Phase 6 — partial**
- README rewritten to be accurate. No demo video, no Grafana dashboard JSON.

**Phase 7 — not started.**
