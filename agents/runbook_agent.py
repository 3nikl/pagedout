"""
PagedOut - Runbook RAG Agent (Phase 5)
Real Qdrant vector search replacing dictionary lookup.
"""

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "runbooks"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
TOP_K = 3

print("Loading RAG components...")
_model = SentenceTransformer(EMBEDDING_MODEL)
_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
print("RAG ready.")


def search_runbooks(query: str, incident_type: str = None, top_k: int = TOP_K):
    vector = _model.encode(query).tolist()

    search_filter = None
    if incident_type and incident_type != "unknown":
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        search_filter = Filter(
            must=[FieldCondition(
                key="incident_type",
                match=MatchValue(value=incident_type)
            )]
        )

    results = _client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        query_filter=search_filter,
        limit=top_k,
        with_payload=True
    )
    return results.points


def runbook_rag_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("📚 RUNBOOK RAG AGENT (Qdrant Vector Search)")
    print("="*50)

    incident_type = state.get('incident_type', 'unknown')
    service = state.get('service', '')
    root_cause = state.get('root_cause', '')
    evidence = state.get('evidence_chain', [])

    query = f"{incident_type} {service} {root_cause}"
    print(f"Query: '{query[:80]}'")
    print(f"Searching {COLLECTION_NAME} collection...")

    # Search with type filter first
    results = search_runbooks(query, incident_type=incident_type, top_k=TOP_K)

    # Fallback — search without filter
    if not results:
        print("No results with filter, searching all runbooks...")
        results = search_runbooks(query, top_k=TOP_K)

    if not results:
        return {
            **state,
            "matched_runbook": "General Incident Response",
            "remediation_steps": [
                "SAFE: Gather logs and metrics",
                "SAFE: Check recent deployments",
                "RISKY: Escalate to senior engineer",
            ],
            "evidence_chain": evidence + ["[RUNBOOK] No matching runbook found."]
        }

    top = results[0]
    runbook = top.payload
    score = top.score

    print(f"\n✅ Top Match:")
    print(f"   Title: {runbook['title']}")
    print(f"   Similarity: {score:.3f}")
    print(f"\n   All matches:")
    for i, r in enumerate(results):
        print(f"   {i+1}. {r.payload['title']} (score: {r.score:.3f})")

    print(f"\n   Steps:")
    for step in runbook['steps']:
        print(f"   - {step}")

    return {
        **state,
        "matched_runbook": runbook['title'],
        "remediation_steps": runbook['steps'],
        "evidence_chain": evidence + [
            f"[RUNBOOK] Found: '{runbook['title']}' similarity={score:.3f}",
            f"[RUNBOOK] Prevention: {runbook.get('prevention', 'N/A')[:100]}"
        ]
    }
