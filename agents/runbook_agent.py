"""
Runbook retrieval agent.

Now backed by the Phase 2 hybrid retriever (dense + BM25, fused with RRF)
over the real 1,342-document corpus, instead of dense-only search over 400
permutations of 20 runbooks.

Why the query is built from the root cause and not just the incident type:
the incident type is one of nine coarse labels, so using it alone makes
every database incident retrieve the same runbook. The investigator's root
cause sentence carries the specific detail — which service, which metric,
which dependency — that makes retrieval discriminate.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The retriever lives in rag/, which is a sibling package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rag"))

from retrieve import HybridRetriever  # noqa: E402

TOP_K = 3

# Built once at import. Loading the embedding model takes ~2s and the BM25
# state is 11k terms; doing that per incident would dominate pipeline latency.
print("Loading hybrid retriever...")
_retriever = HybridRetriever()
print("Retriever ready.")


def runbook_rag_agent(state: dict) -> dict:
    print("\n" + "=" * 50)
    print("📚 RUNBOOK RAG AGENT (hybrid: dense + BM25, RRF)")
    print("=" * 50)

    incident_type = state.get("incident_type", "unknown")
    service = state.get("service", "")
    root_cause = state.get("root_cause", "")
    alert = state.get("alert_title", "")

    query = " ".join(p for p in (alert, root_cause, service) if p).strip() or incident_type
    print(f"Query: {query[:110]!r}")

    # First pass filters to the triaged incident type, which keeps a
    # semantically similar but categorically wrong runbook from outranking
    # the right one.
    hits, ms = _retriever.search_timed(
        query, incident_type=incident_type, top_k=TOP_K, mode="hybrid"
    )

    # The filter can be too narrow — triage may have guessed a type with no
    # runbooks, or mislabelled it. Retry unfiltered rather than returning
    # nothing.
    if not hits:
        print("   no results with incident_type filter, retrying unfiltered...")
        hits, ms = _retriever.search_timed(query, top_k=TOP_K, mode="hybrid")

    if not hits:
        print("   ⚠️  no runbook matched")
        return {
            **state,
            "matched_runbook": "",
            "remediation_steps": [],
            "retrieval_ms": round(ms, 1),
            "evidence_chain": state.get("evidence_chain", []) + [
                "[RUNBOOK] no matching runbook found"
            ],
        }

    print(f"   retrieved {len(hits)} in {ms:.1f}ms")
    for i, h in enumerate(hits, 1):
        print(f"   {i}. [{h.source}] {h.title[:56]} (score {h.score:.3f})")

    # Prefer an actual runbook over a postmortem chunk. Postmortems describe
    # what happened at some other company; runbooks carry executable steps.
    # RRF ranks on relevance alone and has no notion of which is actionable.
    best = next((h for h in hits if h.source == "runbook"), hits[0])
    if best is not hits[0]:
        print(f"   ↪ preferring runbook '{best.title[:50]}' over top postmortem hit")

    print(f"\n✅ Selected: {best.title}")
    for step in best.steps[:8]:
        print(f"     - {step}")

    return {
        **state,
        "matched_runbook": best.title,
        "remediation_steps": best.steps,
        "retrieval_ms": round(ms, 1),
        "retrieved_sources": [
            {"doc_id": h.doc_id, "title": h.title, "source": h.source,
             "score": round(h.score, 4)}
            for h in hits
        ],
        "evidence_chain": state.get("evidence_chain", []) + [
            f"[RUNBOOK] '{best.title}' via hybrid retrieval "
            f"({len(hits)} candidates, {ms:.0f}ms)"
        ],
    }
