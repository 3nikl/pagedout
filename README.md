# PagedOut

### A multi-agent LLM pipeline for SRE incident triage and remediation planning

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-green)
![Qdrant](https://img.shields.io/badge/Qdrant-Vector%20Search-red)
![Ollama](https://img.shields.io/badge/phi3%3Amini-local-orange)
![Status](https://img.shields.io/badge/status-work%20in%20progress-yellow)

> **Status: work in progress.** The agent graph and retrieval layer run end to end locally. The streaming, execution, and evaluation layers are not built yet. This README describes what the code actually does today — see [Not built yet](#not-built-yet) for the honest gap list.

---

## What this is

An on-call engineer paged at 2am spends most of the first hour on retrieval, not repair: finding the right dashboard, the right logs, the right runbook. PagedOut is an experiment in collapsing that step with a stateful multi-agent graph.

An incident signal enters the graph, gets classified by severity and type, gets enriched with investigative context, matched against a runbook corpus via vector search, and turned into a risk-annotated remediation plan with a written postmortem.

It runs entirely locally on a small open model. No API keys, no cloud spend.

---

## What works today

Everything in this section is implemented and runnable. Nothing here is aspirational.

### LangGraph agent pipeline

A compiled `StateGraph` with six nodes and conditional routing, sharing a single typed `PagedOutState` (`agents/state.py`).

| Node | What it actually does |
|---|---|
| **Triage** | Prompts `phi3:mini` for strict JSON, classifies the incident into one of 9 types with a confidence score, and picks the next hop. Brace-matching parse with a safe fallback to escalation on malformed output. |
| **Investigator** | Runs four context-gathering probes (metrics, logs, recent deploys, service dependencies), builds an evidence chain, then asks the LLM for a one-sentence root cause. **The four probes return simulated data** — see [Simulated boundaries](#simulated-boundaries). |
| **Runbook RAG** | Real Qdrant vector search. Embeds the incident context, optionally filters by `incident_type`, retrieves top-3, falls back to an unfiltered search and then to a generic response. |
| **Remediation** | Splits the retrieved runbook steps into low-risk and high-risk by prefix, auto-handles the low-risk ones and queues the rest. **Execution is simulated.** |
| **Postmortem** | Feeds the full evidence chain, actions, and root cause to the LLM and generates a structured report (summary / root cause / impact / actions / prevention). |
| **Escalate** | Terminal branch for low-confidence incidents. Routing sends anything under 0.5 confidence here instead of acting on it. |

Routing is genuinely conditional: `triage → {investigator, runbook, escalate}` based on confidence, then a linear path to `END`.

### Retrieval layer

- **Qdrant** collection, 384-dim vectors, cosine distance, batched upsert.
- **Embeddings** via `sentence-transformers` `all-MiniLM-L6-v2`, running locally.
- **Payload filtering** on `incident_type` to narrow the search space before ranking.
- **Corpus: 400 indexed points, derived from 20 hand-written runbooks.** See [About the corpus](#about-the-corpus) — the honest description matters here.

### Ingestion producer

`pipeline/log_generator.py` builds a `KafkaProducer` and emits synthetic incident telemetry across three topics — `logs.raw`, `metrics.raw`, `alerts.raw` — over 10 service names and 4 incident templates, with P1/P2/P3 severities.

### Fine-tuning dataset (data only)

`finetuning/` contains a working scraper and processor that produced **128 real postmortems** from [danluu/post-mortems](https://github.com/danluu/post-mortems), split 102 train / 26 test in Alpaca instruction format. **No training has been run** — there is no training script in this repo yet.

---

## Simulated boundaries

Being explicit about this, because two agents look more connected to live infrastructure than they are:

- **`investigator_agent.query_prometheus()`** returns values from `random.uniform()`. There is no Prometheus instance. `check_recent_deployments()`, `query_recent_logs()`, and `get_service_dependencies()` return hardcoded strings.
- **`remediation_agent.execute_action()`** prints the action and returns a success string. It does not touch Kubernetes or anything else. The real call site is marked `TODO`.
- **`escalate_agent`** prints `[SIMULATED]` Slack and PagerDuty notifications. No integrations exist.

The graph, the routing, the retrieval, and the LLM reasoning are all real. The I/O at the edges is stubbed.

---

## About the corpus

`rag/index_runbooks.py` contains **20 hand-written runbooks**. It then generates permutations of each — one per service name (12) and one per environment (7) — producing **400 total points**.

Those permutations vary only the title and description string. The `steps`, `symptoms`, and `prevention` fields are identical across a runbook's 20 variants. So the collection holds **400 vectors over 20 distinct documents**.

This is enough to exercise and demo the retrieval path. It is not a realistic corpus, and the point count should not be read as corpus depth. Replacing it with the 128 scraped postmortems already sitting in `finetuning/dataset/` is the top item on the roadmap.

---

## Architecture (as built)

```
pipeline/log_generator.py
         │
         ▼
   Kafka topics ─── logs.raw · metrics.raw · alerts.raw
         │
         ╵  (not yet consumed — see Not built yet)

   test incidents (agents/run_pipeline.py)
         │
         ▼
   ┌─────────────────────────────────────────┐
   │           LangGraph StateGraph          │
   │                                         │
   │   [Triage] ──confidence < 0.5──► [Escalate] ──► END
   │      │                                  │
   │      ├──0.5–0.7──► [Runbook] ───┐       │
   │      │                          │       │
   │      └──> 0.7───► [Investigator]┘       │
   │                          │              │
   │                          ▼              │
   │                      [Runbook] ◄── Qdrant vector search
   │                          │              (400 pts, MiniLM-L6-v2)
   │                          ▼              │
   │                     [Remediate] ── risk split, execution simulated
   │                          │              │
   │                          ▼              │
   │                    [Postmortem] ──► END │
   └─────────────────────────────────────────┘
```

---

## Tech stack (actually used)

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph 1.1 (`StateGraph`, conditional edges, typed state) |
| LLM | `phi3:mini` via Ollama — local, no API key |
| Vector DB | Qdrant (cosine, 384-dim) |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` |
| Streaming | Apache Kafka (producer side only) |
| Dataset | BeautifulSoup scraper → Alpaca-format JSONL |
| Infra | Docker Compose — Kafka, Zookeeper, Qdrant, PostgreSQL, Redis |

PostgreSQL and Redis are running in Compose but are **not yet wired to anything**. They are there for graph checkpointing, which is not implemented.

---

## Quick start

Requires Docker, Python 3.11+, and [Ollama](https://ollama.com).

**1. Start infrastructure**

```bash
docker compose up -d
```

**2. Pull the model**

```bash
ollama pull phi3:mini
```

**3. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**4. Index the runbook corpus into Qdrant**

```bash
python rag/index_runbooks.py
```

**5. Run an incident through the pipeline**

```bash
python agents/run_pipeline.py
```

You'll see each agent print its reasoning to stdout, ending with a generated postmortem. First run downloads the ~80MB embedding model.

Optional — stream synthetic telemetry into Kafka (nothing consumes it yet):

```bash
python pipeline/log_generator.py
```

There is no web dashboard and no HTTP API. `api/` and `frontend/` are empty placeholders.

---

## Project structure

```
pagedout/
├── agents/
│   ├── state.py                        # Typed shared state across all nodes
│   ├── graph.py                        # StateGraph assembly + conditional routing
│   ├── triage_agent.py                 # Severity/type classification
│   ├── investigator_agent.py           # Evidence gathering (probes simulated)
│   ├── runbook_agent.py                # Qdrant retrieval
│   ├── remediation_agent.py            # Risk split (execution simulated)
│   ├── postmortem_escalate_agents.py   # Report generation + escalation branch
│   └── run_pipeline.py                 # Entry point, 2 test incidents
├── rag/
│   ├── index_runbooks.py               # 20 runbooks → 400 Qdrant points
│   └── runbook_agent_v2.py             # Earlier draft of runbook_agent.py
├── pipeline/
│   └── log_generator.py                # Kafka producer, 3 topics
├── finetuning/
│   └── dataset/
│       ├── scraper/github_scraper.py   # danluu/post-mortems scraper
│       ├── processor/dataset_processor.py
│       ├── raw/postmortems_raw.jsonl
│       └── processed/                  # 102 train / 26 test
├── api/                                # empty
├── frontend/                           # empty
├── observability/                      # empty
├── docker-compose.yml
└── requirements.txt
```

---

## Not built yet

Listed so nobody has to go find out by reading the source.

**Streaming**
- No Flink. No stream processor of any kind — the Kafka topics have a producer and no consumer.
- No event normalization schema, windowing, or signal correlation.
- No throughput benchmarking or load-test harness.

**Retrieval**
- No document chunking — each runbook embeds as a single short string.
- No BM25 or hybrid sparse/dense retrieval. Dense only.
- No reranking.
- HNSW is Qdrant's default; it has not been deliberately configured or tuned.
- No retrieval latency or recall measurement.

**Agents**
- No graph checkpointing. `graph.compile()` takes no checkpointer, so a crash mid-flow loses all state.
- The remediation plan is read verbatim from the retrieved runbook payload. No LLM-generated planning, no structured plan schema.

**Safe execution**
- No action registry or allowlist.
- No output schema validation. Risk classification is a string-prefix check on `"SAFE:"` / `"RISKY:"`.
- No dry-run pass, no idempotency keys, no rollback.
- The approval queue is an in-memory list that is printed and discarded. No human is ever actually asked.

**Evaluation**
- No fault injection, no chaos scripts, no automated test runner.
- No accuracy, MTTR, or safety measurements of any kind. **This project currently has no benchmark results.**

**Fine-tuning**
- Dataset preparation only. No training script, no QLoRA/PEFT run, no adapter weights, no evaluation against a baseline.

---

## Roadmap

In rough priority order.

- [x] Docker Compose infrastructure
- [x] Kafka synthetic telemetry producer
- [x] LangGraph 6-node agent graph with conditional routing
- [x] Qdrant vector retrieval
- [x] Postmortem dataset scraping and processing
- [ ] Index the 128 real postmortems as a genuine corpus
- [ ] Structured plan schema — validate LLM output before it becomes an action
- [ ] Action registry with an explicit allowlist
- [ ] Dry-run pass and idempotency keys
- [ ] Graph checkpointing to PostgreSQL
- [ ] Fault-injection harness and an automated eval run
- [ ] Stream processor to consume the Kafka topics
- [ ] Hybrid retrieval (BM25 prefilter) with a measured before/after
- [ ] FastAPI service and dashboard
- [ ] Mistral-7B QLoRA fine-tune on the collected dataset

---

## Design notes

Decisions worth explaining, limited to things actually in the code:

**Why LangGraph over a chain?** Triage needs to divert low-confidence incidents to a human without running the rest of the pipeline. That's a conditional edge on shared state, which a linear chain can't express.

**Why a local 3B model?** `phi3:mini` runs free on a laptop and makes the whole project reproducible by anyone who clones it. The tradeoff is real: phi3:mini is unreliable at tool-calling, which is why `investigator_agent` calls its probes directly in Python and uses the LLM only to summarize the evidence.

**Why parse JSON with brace-matching instead of a structured-output API?** Small local models wrap JSON in prose. Finding the outer `{...}` and falling back to escalation on failure is more robust here than trusting the model to emit clean JSON.

**Why filter Qdrant by `incident_type` before ranking?** Triage has already produced a high-confidence type. Using it as a payload filter keeps a semantically-similar-but-wrong-category runbook from outranking the right one, with an unfiltered retry when the filter is too narrow.

---

## Author

**Nikhil Gade**
[LinkedIn](https://linkedin.com/in/nikhil--gade) · [Email](mailto:nikhilgade.me@gmail.com)

---

*A learning project exploring stateful multi-agent systems and retrieval for incident response. Built in the open, gaps included.*
