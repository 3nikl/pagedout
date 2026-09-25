"""
BM25 as a sparse vector, for hybrid retrieval in Qdrant.

Dense embeddings are good at meaning and bad at rare literal tokens. A query
containing "HikariPool-1" or "exit code 137" should match the runbook that
contains that exact string, but MiniLM maps unusual identifiers to roughly
the same region of space as any other unfamiliar token, so dense search
misses them. BM25 is the opposite: excellent at exact terms, blind to
paraphrase. Running both and fusing is why hybrid beats either alone.

Implementation note — the arithmetic trick that makes this work:

BM25's score for a (query, document) pair is a sum over shared terms. If we
put the ENTIRE per-term BM25 weight on the document side:

    doc_weight(t) = IDF(t) * (tf * (k1 + 1))
                    / (tf + k1 * (1 - b + b * |d| / avgdl))

and put 1.0 on the query side, then the sparse dot product

    sum over shared terms of doc_weight(t) * 1.0

is exactly the BM25 score. So a vector database that only knows how to
compute sparse dot products can compute BM25 without knowing what BM25 is.

Fitted state (IDF table, average document length) is persisted, because
query-time encoding must use the same statistics as index-time encoding.
Recomputing IDF from a single query would be meaningless.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

# Standard BM25 parameters.
#   k1 controls term-frequency saturation: how quickly repeated occurrences
#      of a term stop adding score. 1.5 is the usual default.
#   b  controls length normalisation: 1.0 fully penalises long documents,
#      0.0 ignores length. 0.75 is the usual default.
K1 = 1.5
B = 0.75

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-\.]*")

# Deliberately small. An aggressive stopword list would strip tokens that
# carry real signal in operational text — "no space left on device" loses
# its meaning without "no" and "on".
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "was", "were",
    "that", "this", "it", "as", "at", "by", "for", "with", "be", "been",
    "we", "our", "us", "they", "their", "which", "from", "had", "has",
}


def tokenize(text: str) -> list[str]:
    """Lowercase, keep identifier-ish characters, drop stopwords and 1-char tokens.

    Dots, dashes and underscores are kept inside tokens on purpose: splitting
    them would destroy exactly the strings BM25 is here to catch —
    `pagedout_db_connections_in_use`, `v2.3.1`, `HikariPool-1`.
    """
    return [
        t
        for t in _TOKEN.findall(text.lower())
        if len(t) > 1 and t not in STOPWORDS
    ]


def term_id(term: str) -> int:
    """Stable 31-bit id for a term.

    Python's built-in hash() is randomised per process (PYTHONHASHSEED), so
    using it would mean index-time and query-time ids disagree across runs —
    a bug that would silently return zero sparse matches. blake2b is stable
    across processes and machines.
    """
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFF


class BM25Encoder:
    """Fit IDF over a corpus, then encode documents and queries as sparse vectors."""

    def __init__(self, k1: float = K1, b: float = B):
        self.k1 = k1
        self.b = b
        self.idf: dict[str, float] = {}
        self.avgdl: float = 0.0
        self.n_docs: int = 0

    def fit(self, texts: list[str]) -> "BM25Encoder":
        """Compute document frequencies, IDF and average document length."""
        doc_freq: Counter[str] = Counter()
        total_len = 0

        for text in texts:
            tokens = tokenize(text)
            total_len += len(tokens)
            # set() so a term repeated in one document counts once toward df.
            doc_freq.update(set(tokens))

        self.n_docs = len(texts)
        self.avgdl = total_len / self.n_docs if self.n_docs else 0.0

        # Probabilistic IDF with +0.5 smoothing, and the outer 1 + ... which
        # keeps IDF positive for terms appearing in more than half the corpus.
        # Without it those terms get negative weight and actively push
        # relevant documents down the ranking.
        for term, df in doc_freq.items():
            self.idf[term] = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

        return self

    def encode_document(self, text: str) -> tuple[list[int], list[float]]:
        """Sparse vector carrying the full BM25 weight."""
        tokens = tokenize(text)
        if not tokens:
            return [], []

        tf = Counter(tokens)
        doc_len = len(tokens)
        norm = self.k1 * (1 - self.b + self.b * doc_len / (self.avgdl or 1.0))

        indices: list[int] = []
        values: list[float] = []
        for term, freq in tf.items():
            idf = self.idf.get(term)
            if idf is None:
                # Unseen at fit time; no corpus statistics, so no contribution.
                continue
            weight = idf * (freq * (self.k1 + 1)) / (freq + norm)
            indices.append(term_id(term))
            values.append(weight)
        return indices, values

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        """Sparse vector of 1.0s — all weight lives on the document side."""
        tokens = [t for t in tokenize(text) if t in self.idf]
        if not tokens:
            return [], []
        # dict.fromkeys de-duplicates while preserving order; a repeated query
        # term must not double-count, which is what a plain list would do.
        unique = list(dict.fromkeys(tokens))
        return [term_id(t) for t in unique], [1.0] * len(unique)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "k1": self.k1,
                    "b": self.b,
                    "avgdl": self.avgdl,
                    "n_docs": self.n_docs,
                    "idf": self.idf,
                }
            )
        )

    @classmethod
    def load(cls, path: Path) -> "BM25Encoder":
        data = json.loads(Path(path).read_text())
        enc = cls(k1=data["k1"], b=data["b"])
        enc.avgdl = data["avgdl"]
        enc.n_docs = data["n_docs"]
        enc.idf = data["idf"]
        return enc
