# PagedOut SRE Incident Dataset

A curated dataset of real-world SRE incident postmortems collected from public engineering sources, formatted for fine-tuning Mistral-7B on domain-specific root cause analysis.

---

## Dataset Overview

| Property | Value |
|----------|-------|
| Total Examples | 150+ |
| Train Split | 80% |
| Test Split | 20% |
| Format | Alpaca instruction format (JSONL) |
| Task | Root cause analysis from incident signals |
| Base Model Target | Mistral-7B via QLoRA and PEFT |

---

## Sources

All data collected from publicly available engineering postmortems:

| Source | Description | Count |
|--------|-------------|-------|
| [danluu/post-mortems](https://github.com/danluu/post-mortems) | Curated list of 200+ public postmortems | Primary |
| Cloudflare Engineering Blog | Detailed incident reports with timelines | Secondary |
| AWS Status Page | Public service disruption reports | Secondary |
| Google SRE Book | Example incidents from production systems | Secondary |
| Netflix Tech Blog | Chaos engineering and incident examples | Secondary |

---

## Data Collection Process

### Step 1 — Scraping
`scraper/github_scraper.py` fetches postmortem links from danluu/post-mortems, scrapes each URL, extracts clean text content, and classifies incident type using keyword matching.

### Step 2 — Processing
`processor/dataset_processor.py` filters low quality records, formats each postmortem into Alpaca instruction format, and splits into train/test sets with a fixed random seed for reproducibility.

### Step 3 — Quality Filtering
Records are filtered out if:
- Raw text is less than 300 characters
- No title available
- URL is inaccessible

---

## Data Format

Each example follows the Alpaca instruction format:

```json
{
  "instruction": "You are an expert SRE engineer. Analyze the following incident report and provide a structured root cause analysis with remediation steps.",
  "input": "Incident Title: ...\nSeverity: P1\nIncident Type: database_connection_exhaustion\n\nIncident Summary:\n...\n\nRaw Incident Text:\n...",
  "output": "Root Cause Analysis:\n...\n\nCustomer Impact:\n...\n\nRemediation Steps:\n...\n\nPrevention:\n...",
  "metadata": {
    "id": "pm_0001",
    "source": "danluu/post-mortems",
    "url": "https://...",
    "incident_type": "database_connection_exhaustion",
    "severity": "P1"
  }
}
```

---

## Incident Type Distribution

| Incident Type | Description |
|--------------|-------------|
| database_connection_exhaustion | Connection pool failures, DB timeouts |
| memory_leak | OOM kills, heap exhaustion, GC pressure |
| high_latency_spike | P99 latency spikes, SLA breaches |
| pod_crash_loop | CrashLoopBackOff, OOMKilled containers |
| disk_space_critical | Full disk, write failures |
| network_partition | Split brain, connectivity failures |
| cpu_throttling | CPU limits, thread pool exhaustion |
| deployment_failure | Bad deploys, config regressions |
| cascade_failure | Downstream dependency failures |

---

## Evaluation Methodology

The test set (20%) is used to benchmark fine-tuned Mistral-7B against GPT-4o baseline using RAGAS metrics:

- **Faithfulness** — Is the root cause grounded in the incident signals?
- **Answer Relevance** — Does the output address the incident correctly?
- **Root Cause Correctness** — Does the identified cause match the actual cause?

Results are tracked in `../results/benchmark_results.json`.

---

## Reproducing the Dataset

```bash
# Install dependencies
pip install -r requirements.txt

# Step 1: Scrape raw postmortems
python finetuning/dataset/scraper/github_scraper.py

# Step 2: Process into training format
python finetuning/dataset/processor/dataset_processor.py

# Output files
# finetuning/dataset/raw/postmortems_raw.jsonl
# finetuning/dataset/processed/train.jsonl
# finetuning/dataset/processed/test.jsonl
# finetuning/dataset/processed/dataset_stats.json
```

---

## Ethics and Licensing

All source material is publicly available and intended for educational and research use. No proprietary or confidential data is included. Sources are documented for full transparency and attribution.

---

## Citation

If you use this dataset, please cite the original sources:

- Dan Luu's post-mortems collection: https://github.com/danluu/post-mortems
- Individual engineering blogs as listed in each record's metadata.url field
