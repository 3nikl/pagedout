"""
PagedOut - Dataset Processor
Converts raw scraped postmortems into structured fine-tuning pairs
for Mistral-7B QLoRA training.

Input:  finetuning/dataset/raw/postmortems_raw.jsonl
Output: finetuning/dataset/processed/train.jsonl
        finetuning/dataset/processed/test.jsonl

Usage:
    python finetuning/dataset/processor/dataset_processor.py
"""

import json
import random
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────

RAW_FILE = Path("finetuning/dataset/raw/postmortems_raw.jsonl")
PROCESSED_DIR = Path("finetuning/dataset/processed")
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_FILE = PROCESSED_DIR / "train.jsonl"
TEST_FILE = PROCESSED_DIR / "test.jsonl"
STATS_FILE = PROCESSED_DIR / "dataset_stats.json"

TRAIN_SPLIT = 0.80  # 80% train, 20% test
MIN_ROOT_CAUSE_LENGTH = 50  # Filter out empty root causes
RANDOM_SEED = 42


# ── Formatter ─────────────────────────────────────────────────────────────────

def format_training_example(record: dict) -> dict:
    """
    Converts a raw postmortem record into a fine-tuning training pair.

    Format follows Alpaca instruction format which works well with
    Mistral-7B and QLoRA fine-tuning.
    """
    sections = record.get("sections", {})
    root_cause = sections.get("root_cause", "").strip()
    remediation = sections.get("remediation", "").strip()
    impact = sections.get("impact", "").strip()
    summary = sections.get("summary", "").strip()

    # Build the input context
    instruction = (
        "You are an expert SRE engineer. Analyze the following incident report "
        "and provide a structured root cause analysis with remediation steps."
    )

    input_text = f"""Incident Title: {record.get('title', 'Unknown')}
Severity: {record.get('severity', 'Unknown')}
Incident Type: {record.get('incident_type', 'unknown')}

Incident Summary:
{summary[:500] if summary else 'Not available'}

Raw Incident Text (excerpt):
{record.get('raw_text', '')[:1000]}"""

    output_text = f"""Root Cause Analysis:
{root_cause if root_cause else 'Connection between symptoms and underlying system failure identified through log correlation and metric analysis.'}

Customer Impact:
{impact if impact else 'Service degradation affecting end users during incident window.'}

Remediation Steps:
{remediation if remediation else 'Immediate mitigation applied. Long-term fix deployed and verified.'}

Prevention:
- Add monitoring and alerting for early detection of similar patterns
- Conduct post-incident review and update runbooks
- Implement automated testing to catch regressions"""

    return {
        "instruction": instruction,
        "input": input_text,
        "output": output_text,
        "metadata": {
            "id": record.get("id"),
            "source": record.get("source"),
            "url": record.get("url"),
            "incident_type": record.get("incident_type"),
            "severity": record.get("severity"),
        }
    }


def is_quality_record(record: dict) -> bool:
    """
    Filters out low quality records.
    Returns True if record meets quality bar.
    """
    # Must have raw text
    if not record.get("raw_text"):
        return False

    # Raw text must be substantial
    if len(record.get("raw_text", "")) < 300:
        return False

    # Must have at least a title
    if not record.get("title"):
        return False

    # Skip unknown incident types only if we have too many
    # (we keep some unknowns for diversity)
    return True


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(train: list, test: list) -> dict:
    all_records = train + test

    incident_types = {}
    severities = {}
    sources = {}

    for r in all_records:
        meta = r.get("metadata", {})
        it = meta.get("incident_type", "unknown")
        sv = meta.get("severity", "unknown")
        src = meta.get("source", "unknown")

        incident_types[it] = incident_types.get(it, 0) + 1
        severities[sv] = severities.get(sv, 0) + 1
        sources[src] = sources.get(src, 0) + 1

    return {
        "total_examples": len(all_records),
        "train_examples": len(train),
        "test_examples": len(test),
        "train_split": TRAIN_SPLIT,
        "incident_type_distribution": incident_types,
        "severity_distribution": severities,
        "source_distribution": sources,
        "avg_input_length": int(sum(len(r["input"]) for r in all_records) / max(len(all_records), 1)),
        "avg_output_length": int(sum(len(r["output"]) for r in all_records) / max(len(all_records), 1)),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    if not RAW_FILE.exists():
        print(f"Raw file not found: {RAW_FILE}")
        print("Run github_scraper.py first.")
        return

    print(f"Loading raw postmortems from {RAW_FILE}...")

    raw_records = []
    with open(RAW_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    print(f"Loaded {len(raw_records)} raw records")

    # Quality filter
    quality_records = [r for r in raw_records if is_quality_record(r)]
    print(f"After quality filter: {len(quality_records)} records")

    # Format into training examples
    formatted = [format_training_example(r) for r in quality_records]
    print(f"Formatted {len(formatted)} training examples")

    # Shuffle with fixed seed for reproducibility
    random.seed(RANDOM_SEED)
    random.shuffle(formatted)

    # Train / test split
    split_idx = int(len(formatted) * TRAIN_SPLIT)
    train = formatted[:split_idx]
    test = formatted[split_idx:]

    print(f"Train: {len(train)} examples")
    print(f"Test:  {len(test)} examples")

    # Write train set
    with open(TRAIN_FILE, "w") as f:
        for example in train:
            f.write(json.dumps(example) + "\n")
    print(f"Train set saved to {TRAIN_FILE}")

    # Write test set
    with open(TEST_FILE, "w") as f:
        for example in test:
            f.write(json.dumps(example) + "\n")
    print(f"Test set saved to {TEST_FILE}")

    # Write stats
    stats = compute_stats(train, test)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"Stats saved to {STATS_FILE}")

    print("\n--- Dataset Summary ---")
    print(f"Total examples: {stats['total_examples']}")
    print(f"Train: {stats['train_examples']} | Test: {stats['test_examples']}")
    print(f"Incident types: {stats['incident_type_distribution']}")
    print(f"Severities: {stats['severity_distribution']}")


if __name__ == "__main__":
    run()
