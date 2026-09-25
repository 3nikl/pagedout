"""
Hybrid retrieval: dense embeddings + BM25 sparse, fused with RRF.

Three modes, all available so the benchmark can compare them:

  dense    vector search only. Good at paraphrase, weak on rare literals.
  sparse   BM25 only. Good at exact terms, blind to paraphrase.
  hybrid   both, fused with Reciprocal Rank Fusion. What we actually ship.

Why RRF rather than a weighted score sum:
Cosine similarity lives on [-1, 1]; BM25 scores are unbounded and corpus
dependent. Adding them requires a normalisation constant that has to be
re-tuned whenever the corpus changes. RRF ignores scores entirely and fuses
on RANK:

    score(d) = sum over retrievers of  1 / (k + rank(d))

with k=60 by convention. A document ranked highly by either retriever scores
well; one ranked highly by both wins. No tuning, no scale mismatch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    NamedSparseVector,
    Prefetch,
    SearchParams,
    SparseVector,
)
from sentence_transformers import SentenceTransformer

from bm25 import BM25Encoder

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION = "runbooks"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
BM25_STATE = Path(__file__).resolve().parent / "data" / "bm25_state.json"

# How many candidates each retriever contributes before fusion. Larger gives
# RRF more to work with (better recall) at the cost of more graph traversal.
PREFETCH_LIMIT = 30

# HNSW search-time breadth. This is THE latency/recall knob: higher ef
# explores more of the graph, finds more true neighbours, costs more time.
DEFAULT_EF = 128


@dataclass
class Hit:
    doc_id: str
    title: str
    text: str
    source: str
    incident_type: str
    severity: str
    score: float
    steps: list[str]
    prevention: str
    url: str


class HybridRetriever:
    """Loads the model and BM25 state once, then serves queries.

    Model loading takes ~2s and BM25 state is 11k terms; doing either
    per-query would dominate the latency being measured. Hence a class that
    holds them, not module-level functions that rebuild each call.
    """

    def __init__(
        self,
        collection: str = COLLECTION,
        host: str = QDRANT_HOST,
        port: int = QDRANT_PORT,
    ):
        self.collection = collection
        self.client = QdrantClient(host=host, port=port, timeout=60)
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.bm25 = BM25Encoder.load(BM25_STATE)

    # ── internals ─────────────────────────────────────────────────────────────

    def _filter(self, incident_type: str | None) -> Filter | None:
        if not incident_type or incident_type in ("unknown", "healthy"):
            return None
        return Filter(
            must=[
                FieldCondition(
                    key="incident_type", match=MatchValue(value=incident_type)
                )
            ]
        )

    @staticmethod
    def _to_hits(points) -> list[Hit]:
        hits: list[Hit] = []
        for p in points:
            pl = p.payload or {}
            hits.append(
                Hit(
                    doc_id=pl.get("doc_id", str(p.id)),
                    title=pl.get("title", ""),
                    text=pl.get("text", ""),
                    source=pl.get("source", ""),
                    incident_type=pl.get("incident_type", "unknown"),
                    severity=pl.get("severity", "P2"),
                    score=float(p.score) if p.score is not None else 0.0,
                    steps=pl.get("steps", []),
                    prevention=pl.get("prevention", ""),
                    url=pl.get("url", ""),
                )
            )
        return hits

    # ── public API ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        incident_type: str | None = None,
        top_k: int = 3,
        mode: str = "hybrid",
        ef: int = DEFAULT_EF,
    ) -> list[Hit]:
        qfilter = self._filter(incident_type)
        params = SearchParams(hnsw_ef=ef)

        if mode == "sparse":
            idx, vals = self.bm25.encode_query(query)
            if not idx:
                return []
            res = self.client.query_points(
                collection_name=self.collection,
                query=SparseVector(indices=idx, values=vals),
                using="bm25",
                query_filter=qfilter,
                limit=top_k,
                with_payload=True,
            )
            return self._to_hits(res.points)

        dense_vec = self.model.encode(query).tolist()

        if mode == "dense":
            res = self.client.query_points(
                collection_name=self.collection,
                query=dense_vec,
                using="dense",
                query_filter=qfilter,
                search_params=params,
                limit=top_k,
                with_payload=True,
            )
            return self._to_hits(res.points)

        if mode != "hybrid":
            raise ValueError(f"unknown mode {mode!r}")

        idx, vals = self.bm25.encode_query(query)

        # If the query has no in-vocabulary terms there is nothing for BM25
        # to match, and a sparse prefetch would contribute an empty list.
        # Fall back to dense rather than returning nothing.
        if not idx:
            return self.search(query, incident_type, top_k, "dense", ef)

        res = self.client.query_points(
            collection_name=self.collection,
            prefetch=[
                Prefetch(
                    query=dense_vec,
                    using="dense",
                    limit=PREFETCH_LIMIT,
                    params=params,
                    filter=qfilter,
                ),
                Prefetch(
                    query=SparseVector(indices=idx, values=vals),
                    using="bm25",
                    limit=PREFETCH_LIMIT,
                    filter=qfilter,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return self._to_hits(res.points)

    def search_timed(self, query: str, **kw) -> tuple[list[Hit], float]:
        """Return hits plus total wall-clock milliseconds, embedding included.

        Embedding time is counted deliberately: a user of this system waits
        for it, so excluding it would flatter the number.
        """
        t0 = time.perf_counter()
        hits = self.search(query, **kw)
        return hits, (time.perf_counter() - t0) * 1000.0

    def search_breakdown(self, query: str, **kw) -> tuple[list[Hit], float, float]:
        """Return hits, embedding ms, and search ms separately.

        Worth splitting because the two costs scale with completely different
        things: embedding is a fixed per-query model forward pass, while
        search grows with corpus size and index configuration. Reporting only
        the total hides which one an optimisation actually moved.
        """
        mode = kw.get("mode", "hybrid")

        t0 = time.perf_counter()
        if mode != "sparse":
            self.model.encode(query)
        embed_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        hits = self.search(query, **kw)
        total_ms = (time.perf_counter() - t1) * 1000.0

        # `search` re-encodes internally, so subtract that to isolate the
        # database round trip rather than double-counting the model.
        search_ms = max(0.0, total_ms - embed_ms)
        return hits, embed_ms, search_ms


if __name__ == "__main__":
    r = HybridRetriever()
    demos = [
        ("connection pool exhausted, HikariPool timeout", None),
        ("heap at 94 percent and GC overhead limit exceeded", None),
        ("exit code 137 container killed", None),
    ]
    for q, itype in demos:
        print(f"\n=== {q!r} ===")
        for mode in ("dense", "sparse", "hybrid"):
            hits, ms = r.search_timed(q, incident_type=itype, top_k=3, mode=mode)
            top = hits[0].title[:58] if hits else "(none)"
            print(f"  {mode:7} {ms:6.1f}ms  top: {top}")
