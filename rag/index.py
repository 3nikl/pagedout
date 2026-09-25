"""
Build the Qdrant index: dense vectors + BM25 sparse vectors + HNSW config.

Creates two collections so the Phase 2 benchmark has something to compare
against:

  runbooks          the real index. HNSW enabled, dense + sparse vectors.
  runbooks_exact    same data, HNSW DISABLED. Every query is a brute-force
                    scan over all 1,342 vectors.

The second one exists purely so "we made retrieval faster" is a measurement
rather than an assertion. Without a baseline, a latency number means nothing.

Usage:
    python rag/index.py            # build both collections
    python rag/index.py --fast     # skip the exact baseline collection
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PointStruct,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)
from sentence_transformers import SentenceTransformer

from bm25 import BM25Encoder
from corpus import build_corpus

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION = "runbooks"
COLLECTION_EXACT = "runbooks_exact"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_SIZE = 384
BM25_STATE = Path(__file__).resolve().parent / "data" / "bm25_state.json"

# HNSW parameters, chosen deliberately rather than left at defaults.
#
#   m = 32              edges per node. Higher m means better recall and more
#                       memory. Qdrant's default is 16; this corpus is small
#                       (1.3k vectors, ~2 MB) so we can afford 32 and take
#                       the recall.
#
#   ef_construct = 200  size of the candidate list while BUILDING the graph.
#                       Higher builds a better graph and costs index time
#                       only, never query time. Default is 100.
#
#   ef (search-time)    set per query in retrieve.py, because it is the knob
#                       that trades latency against recall at request time.
HNSW_M = 32
HNSW_EF_CONSTRUCT = 200


def connect() -> QdrantClient:
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=120)


def _create(client: QdrantClient, name: str, hnsw: bool) -> None:
    """Create a collection, optionally with HNSW disabled for the baseline."""
    client.delete_collection(name)

    # m=0 is Qdrant's switch for "no HNSW graph" — every search becomes an
    # exact scan. That is precisely the baseline we want to measure against.
    hnsw_config = (
        HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT)
        if hnsw
        else HnswConfigDiff(m=0)
    )

    client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
                hnsw_config=hnsw_config,
            )
        },
        sparse_vectors_config={
            "bm25": SparseVectorParams(
                # IDF weighting already lives in the stored values, so Qdrant
                # must not apply its own on top — that would square the IDF.
                index=SparseIndexParams(on_disk=False)
            )
        },
        optimizers_config=OptimizersConfigDiff(
            # Build the index immediately instead of waiting for the default
            # 20k-vector threshold. Our corpus is smaller than that, so
            # without this the "HNSW" collection would never actually build
            # a graph and the benchmark would compare exact against exact.
            indexing_threshold=100
        ),
    )


def build(include_exact: bool = True) -> dict:
    print("Loading corpus...")
    corpus = build_corpus()
    texts = [d.text for d in corpus]
    print(f"  {len(corpus)} documents")

    print(f"\nFitting BM25 over the corpus...")
    t0 = time.perf_counter()
    encoder = BM25Encoder().fit(texts)
    encoder.save(BM25_STATE)
    print(f"  vocabulary: {len(encoder.idf):,} terms")
    print(f"  avg doc length: {encoder.avgdl:.1f} tokens")
    print(f"  fitted in {time.perf_counter() - t0:.2f}s -> {BM25_STATE.name}")

    print(f"\nLoading embedding model ({EMBEDDING_MODEL})...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    print("Embedding corpus...")
    t0 = time.perf_counter()
    # batch_size is a throughput/memory trade; 64 keeps peak RSS modest on a
    # machine where RAM is the binding constraint.
    dense = model.encode(
        texts, batch_size=64, show_progress_bar=False, convert_to_numpy=True
    )
    embed_secs = time.perf_counter() - t0
    print(f"  {len(dense)} vectors in {embed_secs:.1f}s "
          f"({len(dense) / embed_secs:.0f} docs/sec)")

    print("\nBuilding points...")
    points: list[PointStruct] = []
    for i, (doc, vec) in enumerate(zip(corpus, dense)):
        idx, vals = encoder.encode_document(doc.text)
        points.append(
            PointStruct(
                id=i,
                vector={
                    "dense": vec.tolist(),
                    "bm25": SparseVector(indices=idx, values=vals),
                },
                payload=doc.payload(),
            )
        )

    client = connect()
    targets = [(COLLECTION, True)] + ([(COLLECTION_EXACT, False)] if include_exact else [])

    stats = {}
    for name, hnsw in targets:
        label = "HNSW" if hnsw else "exact (baseline)"
        print(f"\nIndexing '{name}' [{label}]...")
        _create(client, name, hnsw)

        t0 = time.perf_counter()
        for start in range(0, len(points), 128):
            client.upsert(
                collection_name=name, points=points[start : start + 128], wait=True
            )
        took = time.perf_counter() - t0

        info = client.get_collection(name)
        stats[name] = {
            "points": info.points_count,
            "indexed_vectors": info.indexed_vectors_count,
            "index_seconds": round(took, 2),
        }
        print(f"  {info.points_count} points, "
              f"{info.indexed_vectors_count} indexed vectors, {took:.1f}s")

    return {
        "documents": len(corpus),
        "vocabulary": len(encoder.idf),
        "collections": stats,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="skip the exact baseline")
    args = ap.parse_args()

    result = build(include_exact=not args.fast)
    print("\n" + "=" * 60)
    print(f"Corpus indexed: {result['documents']} documents")
    print(f"BM25 vocabulary: {result['vocabulary']:,} terms")
    for name, s in result["collections"].items():
        print(f"  {name:18} {s['points']:>6} points  {s['index_seconds']:>6.1f}s")
