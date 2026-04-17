"""
PagedOut - Runbook RAG Agent (Phase 5)
Replaced dictionary lookup with real Qdrant vector search.
Uses sentence-transformers for local free embeddings.
"""

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

# ── CONFIG ────────────────────────────────────────────────────────────────────

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "runbooks"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
TOP_K = 3

# Load model once at module level
print("Loading embedding model for RAG...")
_model = SentenceTransformer(EMBEDDING_MODEL)
_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
print("RAG ready.")


# ── SEARCH FUNCTION ───────────────────────────────────────────────────────────

def search_runbooks(query: str, incident_type: str = None, top_k: int = TOP_K) -> list:
    """
    Search Qdrant for most relevant runbooks.
    Uses semantic similarity search.
    Optionally filters by incident_type.
    """
    # Generate query embedding
    query_vector = _model.encode(query).tolist()

    # Build filter if incident_type provided
    search_filter = None
    if incident_type and incident_type != "unknown":
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        search_filter = Filter(
            must=[
                FieldCondition(
                    key="incident_type",
                    match=MatchValue(value=incident_type)
                )
            ]
        )

    # Search Qdrant
    results = _client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        query_filter=search_filter,
        limit=top_k,
        with_payload=True
    )

    return results


# ── RUNBOOK RAG AGENT ─────────────────────────────────────────────────────────

def runbook_rag_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("📚 RUNBOOK RAG AGENT (Qdrant Phase 5)")
    print("="*50)

    incident_type = state.get('incident_type', 'unknown')
    service = state.get('service', '')
    root_cause = state.get('root_cause', '')
    evidence = state.get('evidence_chain', [])

    # Build search query from incident context
    query = f"{incident_type} {service} {root_cause}"
    print(f"Query: '{query[:80]}'")
    print(f"Searching Qdrant collection '{COLLECTION_NAME}'...")

    # Search with incident type filter first
    results = search_runbooks(query, incident_type=incident_type, top_k=TOP_K)

    # If no results with filter, search without filter
    if not results:
        print("   No results with type filter, searching all runbooks...")
        results = search_runbooks(query, top_k=TOP_K)

    if not results:
        print("   No runbooks found. Using generic response.")
        return {
            **state,
            "matched_runbook": "General Incident Response",
            "remediation_steps": [
                "SAFE: Gather logs and metrics",
                "SAFE: Check recent deployments",
                "RISKY: Escalate to senior engineer",
            ],
            "evidence_chain": evidence + ["[RUNBOOK] No matching runbook found. Using generic response."]
        }

    # Use top result
    top_result = results[0]
    runbook = top_result.payload
    score = top_result.score

    print(f"\n✅ Top Runbook Match:")
    print(f"   Title: {runbook['title']}")
    print(f"   Similarity Score: {score:.3f}")
    print(f"   Incident Type: {runbook['incident_type']}")
    print(f"\n   All matches:")
    for i, r in enumerate(results):
        print(f"   {i+1}. {r.payload['title']} (score: {r.score:.3f})")

    print(f"\n   Remediation Steps:")
    for step in runbook['steps']:
        print(f"   - {step}")

    return {
        **state,
        "matched_runbook": runbook['title'],
        "remediation_steps": runbook['steps'],
        "evidence_chain": evidence + [
            f"[RUNBOOK] Found: '{runbook['title']}' with similarity {score:.3f}",
            f"[RUNBOOK] Prevention: {runbook.get('prevention', 'N/A')[:100]}"
        ]
    }
