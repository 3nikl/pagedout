"""
PagedOut - GitHub Postmortem Scraper
Scrapes public SRE incident postmortems from danluu/post-mortems
and formats them for fine-tuning dataset collection.

Sources:
- https://github.com/danluu/post-mortems (200+ public postmortems)
- Direct blog URLs from engineering teams

Usage:
    python finetuning/dataset/scraper/github_scraper.py
"""

import json
import time
import re
import os
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
OUTPUT_FILE = Path("finetuning/dataset/raw/postmortems_raw.jsonl")
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "PagedOut-Dataset-Collector",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"

# Incident type keywords for auto classification
INCIDENT_KEYWORDS = {
    "database_connection_exhaustion": [
        "connection pool", "connection exhausted", "too many connections",
        "db connection", "database connection"
    ],
    "memory_leak": [
        "memory leak", "oom", "out of memory", "heap exhausted",
        "gc pressure", "memory exhausted"
    ],
    "high_latency_spike": [
        "latency spike", "slow response", "timeout", "high latency",
        "response time", "p99", "p95"
    ],
    "pod_crash_loop": [
        "crash loop", "crashloopbackoff", "pod restart", "oomkilled",
        "container crash", "pod failure"
    ],
    "disk_space_critical": [
        "disk full", "no space left", "disk usage", "storage full",
        "disk space", "inode"
    ],
    "network_partition": [
        "network partition", "split brain", "network failure",
        "connectivity", "packet loss", "network timeout"
    ],
    "cpu_throttling": [
        "cpu throttl", "cpu limit", "cpu spike", "cpu usage",
        "cpu pressure", "throttling"
    ],
    "deployment_failure": [
        "deployment", "rollback", "bad deploy", "release",
        "regression", "config change"
    ],
    "cascade_failure": [
        "cascade", "cascading", "domino", "downstream",
        "dependency failure", "service mesh"
    ],
}

# ── Classifier ────────────────────────────────────────────────────────────────

def classify_incident(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for incident_type, keywords in INCIDENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[incident_type] = score
    if not scores:
        return "unknown"
    return max(scores, key=scores.get)


def estimate_severity(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["outage", "down", "unavailable", "fatal", "critical", "data loss"]):
        return "P1"
    elif any(w in text_lower for w in ["degraded", "slow", "partial", "intermittent", "elevated error"]):
        return "P2"
    else:
        return "P3"


# ── Scraper ───────────────────────────────────────────────────────────────────

def fetch_danluu_postmortems() -> list[dict]:
    """
    Fetches the list of postmortem links from danluu/post-mortems README.
    Returns a list of dicts with title and url.
    """
    print("Fetching postmortem list from danluu/post-mortems...")
    url = "https://api.github.com/repos/danluu/post-mortems/contents/README.md"

    response = requests.get(url, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        print(f"Failed to fetch README: {response.status_code}")
        return []

    import base64
    content = base64.b64decode(response.json()["content"]).decode("utf-8")

    # Extract markdown links [title](url)
    links = re.findall(r'\[([^\]]+)\]\((https?://[^\)]+)\)', content)

    postmortems = []
    for title, url in links:
        if any(domain in url for domain in [
            "blog", "engineering", "medium", "github", "incident",
            "postmortem", "status", "outage", "rootcause"
        ]):
            postmortems.append({"title": title, "url": url})

    print(f"Found {len(postmortems)} postmortem links")
    return postmortems


def scrape_url(url: str, timeout: int = 15) -> Optional[str]:
    """
    Scrapes text content from a URL.
    Returns cleaned text or None if failed.
    """
    try:
        response = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PagedOut-Scraper/1.0)"
        })
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove nav, footer, ads, scripts
        for tag in soup(["nav", "footer", "script", "style", "header", "aside"]):
            tag.decompose()

        # Get main content
        main = soup.find("main") or soup.find("article") or soup.find("body")
        if not main:
            return None

        text = main.get_text(separator="\n", strip=True)

        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        # Must be substantial content
        if len(text) < 200:
            return None

        return text[:8000]  # Cap at 8000 chars

    except Exception as e:
        return None


def extract_sections(text: str) -> dict:
    """
    Extracts key sections from postmortem text.
    Looks for common postmortem section headers.
    """
    sections = {
        "summary": "",
        "timeline": "",
        "root_cause": "",
        "impact": "",
        "remediation": "",
        "prevention": "",
    }

    text_lower = text.lower()

    # Root cause patterns
    for pattern in ["root cause", "cause", "what happened", "contributing factors"]:
        idx = text_lower.find(pattern)
        if idx != -1:
            excerpt = text[idx:idx+500].strip()
            sections["root_cause"] = excerpt
            break

    # Remediation patterns
    for pattern in ["remediation", "resolution", "fix", "mitigation", "action items"]:
        idx = text_lower.find(pattern)
        if idx != -1:
            excerpt = text[idx:idx+500].strip()
            sections["remediation"] = excerpt
            break

    # Impact patterns
    for pattern in ["impact", "affected", "users affected", "customer impact"]:
        idx = text_lower.find(pattern)
        if idx != -1:
            excerpt = text[idx:idx+300].strip()
            sections["impact"] = excerpt
            break

    # Summary — first 300 chars usually
    sections["summary"] = text[:300].strip()

    return sections


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    postmortems = fetch_danluu_postmortems()
    if not postmortems:
        print("No postmortems found. Check your internet connection.")
        return

    results = []
    failed = 0
    success = 0

    print(f"\nScraping {len(postmortems)} postmortems...")
    print("This will take a few minutes. Be patient.\n")

    for i, pm in enumerate(postmortems[:200]):  # Cap at 200
        print(f"[{i+1}/{min(len(postmortems), 200)}] {pm['title'][:60]}...")

        text = scrape_url(pm["url"])
        if not text:
            failed += 1
            continue

        sections = extract_sections(text)
        incident_type = classify_incident(text)
        severity = estimate_severity(text)

        record = {
            "id": f"pm_{i+1:04d}",
            "title": pm["title"],
            "url": pm["url"],
            "incident_type": incident_type,
            "severity": severity,
            "raw_text": text,
            "sections": sections,
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "danluu/post-mortems",
        }

        results.append(record)
        success += 1

        # Write incrementally so we don't lose progress
        with open(OUTPUT_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Polite delay to avoid rate limiting
        time.sleep(1.5)

    print(f"\nDone. Success: {success} | Failed: {failed}")
    print(f"Raw dataset saved to {OUTPUT_FILE}")
    print(f"Total records: {success}")


if __name__ == "__main__":
    run()
