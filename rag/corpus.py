"""
Corpus construction for PagedOut retrieval.

Replaces the previous approach, which took 20 hand-written runbooks and
generated 400 "documents" by pasting a service name into the title. Those
permutations shared identical steps and symptoms, so the index held 400
vectors over 20 distinct documents — a number that looked impressive and
meant nothing.

This module builds a corpus from two real sources:

  1. 129 public postmortems scraped from danluu/post-mortems. Long prose,
     so they are chunked.
  2. 20 hand-written operational runbooks. Short and already structured,
     so they are indexed whole — chunking them would split a procedure
     away from the symptoms that identify it.

Every document keeps provenance (source, url, doc_id) so a retrieval result
can be traced back to where it came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTMORTEMS = ROOT / "finetuning" / "dataset" / "raw" / "postmortems_raw.jsonl"
RUNBOOKS = ROOT / "rag" / "data" / "runbooks.json"

# MiniLM-L6-v2 truncates at 256 word-pieces, roughly 1000 characters of
# English prose. Chunking at 800 keeps whole chunks inside that window with
# headroom, so no chunk is silently truncated at embed time.
CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
MIN_CHUNK_CHARS = 120


@dataclass
class Document:
    """One indexable unit."""

    doc_id: str
    text: str            # what gets embedded
    title: str
    source: str          # 'postmortem' | 'runbook'
    incident_type: str
    severity: str
    url: str = ""
    chunk_index: int = 0
    total_chunks: int = 1
    steps: list[str] = field(default_factory=list)
    prevention: str = ""

    def payload(self) -> dict:
        """Everything stored alongside the vector in Qdrant."""
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "incident_type": self.incident_type,
            "severity": self.severity,
            "url": self.url,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "steps": self.steps,
            "prevention": self.prevention,
        }


# ── Chunking ──────────────────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_text(
    text: str,
    size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split prose into overlapping, sentence-aligned chunks.

    Why sentence-aligned rather than a fixed character slice: cutting
    mid-sentence produces a fragment whose embedding sits in a meaningless
    part of the vector space. Retrieval then matches on half a thought.

    Why overlapping: a fact that straddles a boundary would otherwise appear
    in neither chunk with enough surrounding context to be retrievable. The
    overlap means every span of ~150 chars appears in two chunks.

    The greedy pack-then-flush loop below is used instead of a naive
    fixed-stride slice because sentences vary wildly in length, so a fixed
    stride would produce chunks anywhere from 1 to 5 sentences.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= size:
        return [text] if len(text) >= MIN_CHUNK_CHARS else []

    sentences = _SENTENCE_END.split(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # A single sentence longer than the window cannot be packed; split it
        # on whitespace rather than dropping it.
        if len(sentence) > size:
            if current:
                chunks.append(" ".join(current))
                current, current_len = [], 0
            for i in range(0, len(sentence), size - overlap):
                piece = sentence[i : i + size]
                if len(piece) >= MIN_CHUNK_CHARS:
                    chunks.append(piece)
            continue

        if current_len + len(sentence) + 1 > size:
            chunks.append(" ".join(current))
            # Re-seed the next chunk with trailing sentences worth ~overlap
            # characters, so context carries across the boundary.
            carry: list[str] = []
            carry_len = 0
            for prev in reversed(current):
                if carry_len + len(prev) > overlap:
                    break
                carry.insert(0, prev)
                carry_len += len(prev) + 1
            current, current_len = carry, carry_len

        current.append(sentence)
        current_len += len(sentence) + 1

    if current:
        tail = " ".join(current)
        if len(tail) >= MIN_CHUNK_CHARS:
            chunks.append(tail)

    return chunks


# ── Loaders ───────────────────────────────────────────────────────────────────


def load_postmortems() -> list[Document]:
    """Chunk every scraped postmortem into indexable documents."""
    if not POSTMORTEMS.exists():
        raise FileNotFoundError(f"missing {POSTMORTEMS}")

    docs: list[Document] = []
    for line in POSTMORTEMS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        body = (rec.get("raw_text") or "").strip()
        if len(body) < MIN_CHUNK_CHARS:
            continue

        title = (rec.get("title") or "untitled").strip()
        pieces = chunk_text(body)
        for i, piece in enumerate(pieces):
            docs.append(
                Document(
                    doc_id=f"{rec.get('id', 'pm')}#{i}",
                    # Prefixing the title gives every chunk a topical anchor.
                    # Without it, chunk 7 of a Cloudflare postmortem embeds as
                    # generic prose with nothing tying it to the incident.
                    text=f"{title}. {piece}",
                    title=title,
                    source="postmortem",
                    incident_type=rec.get("incident_type") or "unknown",
                    severity=rec.get("severity") or "P2",
                    url=rec.get("url", ""),
                    chunk_index=i,
                    total_chunks=len(pieces),
                )
            )
    return docs


def load_runbooks() -> list[Document]:
    """Load runbooks as whole documents, one per runbook.

    Deliberately not chunked: a runbook's symptoms and its remediation steps
    have to stay in the same document, or retrieval can surface a procedure
    without the symptoms that justify it.
    """
    if not RUNBOOKS.exists():
        raise FileNotFoundError(f"missing {RUNBOOKS}")

    docs: list[Document] = []
    for i, rb in enumerate(json.loads(RUNBOOKS.read_text())):
        symptoms = " ".join(rb.get("symptoms", []))
        steps = rb.get("steps", [])
        text = (
            f"{rb['title']}. {rb['description']} "
            f"Symptoms: {symptoms}. "
            f"Remediation: {' '.join(steps)}"
        )
        docs.append(
            Document(
                doc_id=f"rb_{i:03d}",
                text=text,
                title=rb["title"],
                source="runbook",
                incident_type=rb["incident_type"],
                severity=rb.get("severity", "P2"),
                chunk_index=0,
                total_chunks=1,
                steps=steps,
                prevention=rb.get("prevention", ""),
            )
        )
    return docs


def build_corpus() -> list[Document]:
    """The full corpus: runbooks first, then postmortem chunks."""
    return load_runbooks() + load_postmortems()


if __name__ == "__main__":
    import statistics
    from collections import Counter

    corpus = build_corpus()
    lengths = [len(d.text) for d in corpus]
    by_source = Counter(d.source for d in corpus)

    print(f"Corpus: {len(corpus)} documents")
    print(f"  runbooks   : {by_source['runbook']}")
    print(f"  postmortems: {by_source['postmortem']} chunks "
          f"from {len({d.doc_id.split('#')[0] for d in corpus if d.source == 'postmortem'})} sources")
    print(f"  chars: min={min(lengths)} median={int(statistics.median(lengths))} "
          f"max={max(lengths)}")
    print(f"\n  by incident_type:")
    for t, n in Counter(d.incident_type for d in corpus).most_common():
        print(f"    {t:34} {n:>5}")
