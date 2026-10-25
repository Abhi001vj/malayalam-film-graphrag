"""Optional local semantic index. Exact cosine scan is suitable for the MVP only."""
from __future__ import annotations

from array import array
from functools import lru_cache
from hashlib import sha256
from math import sqrt
import sqlite3

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS document_vectors (
        document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
        model TEXT NOT NULL, text_hash TEXT NOT NULL, dimensions INTEGER NOT NULL,
        embedding BLOB NOT NULL
    )""")


@lru_cache(maxsize=1)
def _model():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("Install the vector extra: pip install -e '.[vectors]'") from exc
    return SentenceTransformer(MODEL)


def build_vectors(con: sqlite3.Connection) -> int:
    _schema(con)
    rows = con.execute("SELECT id,text FROM documents ORDER BY id").fetchall()
    existing = {r["document_id"]: r["text_hash"] for r in
                con.execute("SELECT document_id,text_hash FROM document_vectors WHERE model=?", (MODEL,))}
    pending = [(r["id"], r["text"], sha256(r["text"].encode()).hexdigest())
               for r in rows if existing.get(r["id"]) != sha256(r["text"].encode()).hexdigest()]
    if not pending:
        return 0
    embeddings = _model().encode([x[1] for x in pending], normalize_embeddings=True,
                                 show_progress_bar=len(pending) > 50)
    for (doc_id, _, digest), vector in zip(pending, embeddings):
        floats = array("f", (float(v) for v in vector))
        con.execute("""INSERT INTO document_vectors(document_id,model,text_hash,dimensions,embedding)
            VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
            model=excluded.model,text_hash=excluded.text_hash,
            dimensions=excluded.dimensions,embedding=excluded.embedding""",
            (doc_id, MODEL, digest, len(floats), floats.tobytes()))
    con.commit()
    return len(pending)


def vector_scores(con: sqlite3.Connection, query: str, film_ids: set[str]) -> dict[str, float]:
    if not film_ids:
        return {}
    _schema(con)
    if not con.execute("SELECT 1 FROM document_vectors WHERE model=? LIMIT 1", (MODEL,)).fetchone():
        return {}
    query_vector = [float(x) for x in _model().encode(query, normalize_embeddings=True)]
    scores: dict[str, float] = {}
    rows = con.execute("""SELECT d.film_id,v.dimensions,v.embedding FROM document_vectors v
        JOIN documents d ON d.id=v.document_id WHERE v.model=?""", (MODEL,))
    for row in rows:
        fid = row["film_id"]
        if fid not in film_ids or row["dimensions"] != len(query_vector):
            continue
        vector = array("f")
        vector.frombytes(row["embedding"])
        cosine = sum(a * b for a, b in zip(query_vector, vector))
        scores[fid] = max(scores.get(fid, -1.0), cosine)
    return scores
