"""Unstructured side of GraphRAG: chunking, embeddings, BM25 and hybrid (RRF) retrieval.

Lexical graph: (:Chunk)-[:PART_OF]->(:Film). Chunks are paragraphs of the lead, plot and
production sections, capped at ~900 characters with sentence-aware splitting.
Dense vectors come from a local Ollama embedding model and live in a NumPy matrix
(exact cosine; 20k x 768 floats is ~60 MB) and, when Neo4j is used, in its vector index.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import llm

CHUNK_KINDS = ('lead', 'plot', 'plot summary', 'plot synopsis', 'plots', 'production', 'development',
               'filming', 'casting and filming', 'pre-production', 'themes and production')
MAX_CHARS = 900
RRF_K = 10
# Dense evidence counts double: on paraphrased plot questions BM25 mostly adds noise, but it still rescues
# exact names/rare terms. Chosen on evals/plot_gold.jsonl (29 questions) -- see evals/report.md.
RRF_WEIGHTS = (1.0, 0.5)
VECTORS = Path('data/vectors')  # default; the settings page can point elsewhere


def _vectors() -> Path:
    from . import settings
    return Path(settings.get('paths.vectors_dir'))


def _split(text: str) -> list[str]:
    chunks = []
    for para in re.split(r'\n\s*\n|\n', text):
        para = para.strip()
        while len(para) > MAX_CHARS:
            cut = para.rfind('. ', 0, MAX_CHARS)
            cut = cut + 1 if cut > MAX_CHARS // 3 else MAX_CHARS
            chunks.append(para[:cut].strip()); para = para[cut:].strip()
        if len(para) >= 60:
            chunks.append(para)
    return chunks


def build_chunks(con: sqlite3.Connection) -> int:
    con.executescript("""DROP TABLE IF EXISTS kg_chunks; DROP TABLE IF EXISTS kg_chunks_fts;
      CREATE TABLE kg_chunks(id INTEGER PRIMARY KEY, film_id TEXT NOT NULL, kind TEXT NOT NULL,
        seq INTEGER NOT NULL, text TEXT NOT NULL, source_url TEXT NOT NULL);
      CREATE INDEX kg_chunks_film ON kg_chunks(film_id);
      CREATE VIRTUAL TABLE kg_chunks_fts USING fts5(text, content='kg_chunks', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2');""")
    rows = con.execute(f"""SELECT m.canonical_id, d.kind, d.text, d.source_url FROM documents d
        JOIN kg_film_map m ON m.film_id=d.film_id WHERE d.kind IN ({','.join('?' * len(CHUNK_KINDS))})
        ORDER BY m.canonical_id, d.kind""", CHUNK_KINDS).fetchall()
    seen = set()
    for film_id, kind, text, url in rows:
        for seq, chunk in enumerate(_split(text)):
            key = (film_id, chunk[:200])
            if key in seen:  # duplicate source pages for merged films
                continue
            seen.add(key)
            con.execute('INSERT INTO kg_chunks(film_id,kind,seq,text,source_url) VALUES(?,?,?,?,?)',
                        (film_id, 'plot' if kind.startswith('plot') else kind, seq, chunk, url))
    con.execute("INSERT INTO kg_chunks_fts(kg_chunks_fts) VALUES('rebuild')")
    con.commit()
    return con.execute('SELECT count(*) FROM kg_chunks').fetchone()[0]


def build_embeddings(con: sqlite3.Connection, batch: int = 64, log=print) -> int:
    vectors = _vectors()
    vectors.mkdir(parents=True, exist_ok=True)
    rows = con.execute('SELECT id, text FROM kg_chunks ORDER BY id').fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    matrix = np.zeros((len(rows), 768), dtype=np.float32)
    started = time.time()
    for start in range(0, len(rows), batch):
        part = rows[start:start + batch]
        # Task prefix recommended for embeddinggemma document embeddings.
        matrix[start:start + len(part)] = llm.embed(['title: none | text: ' + r[1] for r in part])
        if start // batch % 20 == 0:
            log(f'Embedded {start + len(part)}/{len(rows)} chunks ({time.time() - started:.0f}s)')
    np.save(vectors / 'chunk_ids.npy', ids)
    np.save(vectors / 'chunk_vectors.npy', matrix)
    _index.cache_clear()
    return len(rows)


_cache: dict = {}


def _index():
    if 'm' not in _cache:
        vectors = _vectors()
        path = vectors / 'chunk_vectors.npy'
        _cache['m'] = (np.load(vectors / 'chunk_ids.npy'), np.load(path)) if path.exists() else (None, None)
    return _cache['m']
_index.cache_clear = lambda: _cache.clear()


def embed_query(text: str) -> np.ndarray:
    return np.asarray(llm.embed(['task: search result | query: ' + text])[0], dtype=np.float32)


def vector_search(query: str, k: int = 50, film_ids: set[str] | None = None, con=None) -> list[tuple[int, float]]:
    ids, matrix = _index()
    if ids is None:
        return []
    scores = matrix @ embed_query(query)
    if film_ids is not None and con is not None:
        allowed = {r[0] for r in con.execute(
            f"SELECT id FROM kg_chunks WHERE film_id IN ({','.join('?' * len(film_ids))})", list(film_ids))}
        mask = np.isin(ids, list(allowed))
        scores = np.where(mask, scores, -1)
    top = np.argsort(-scores)[:k]
    return [(int(ids[i]), float(scores[i])) for i in top if scores[i] > -1]


def bm25_search(con: sqlite3.Connection, query: str, k: int = 50, film_ids: set[str] | None = None) -> list[tuple[int, float]]:
    terms = [t for t in re.findall(r'\w+', query.lower()) if len(t) > 2 and t not in _STOP]
    if not terms:
        return []
    expression = ' OR '.join('"' + t + '"' for t in terms)
    sql = 'SELECT f.rowid, bm25(kg_chunks_fts) FROM kg_chunks_fts f'
    params: list = [expression]
    if film_ids is not None:
        sql += f" JOIN kg_chunks c ON c.id=f.rowid WHERE kg_chunks_fts MATCH ? AND c.film_id IN ({','.join('?' * len(film_ids))})"
        params += list(film_ids)
    else:
        sql += ' WHERE kg_chunks_fts MATCH ?'
    return [(r[0], -r[1]) for r in con.execute(sql + ' ORDER BY bm25(kg_chunks_fts) LIMIT ?', [*params, k])]


def rrf(*rankings: list[tuple[int, float]], k: int = RRF_K, weights=None) -> list[tuple[int, float]]:
    """(Weighted) Reciprocal Rank Fusion: score = sum w/(k + rank). Robust to incomparable score scales."""
    fused: dict[int, float] = {}
    for ranking, weight in zip(rankings, weights or [1.0] * len(rankings)):
        for rank, (cid, _) in enumerate(ranking):
            fused[cid] = fused.get(cid, 0) + weight / (k + rank + 1)
    return sorted(fused.items(), key=lambda x: -x[1])


def hybrid_search(con: sqlite3.Connection, query: str, k: int = 10, film_ids: set[str] | None = None,
                  mode: str = 'hybrid') -> dict:
    """Chunk retrieval -> film ranking (max chunk score). `film_ids` is the graph pre-filter."""
    timings = {}
    t = time.perf_counter()
    dense = vector_search(query, 60, film_ids, con) if mode in ('hybrid', 'vector') else []
    timings['vector_ms'] = round((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    sparse = bm25_search(con, query, 60, film_ids) if mode in ('hybrid', 'bm25') else []
    timings['bm25_ms'] = round((time.perf_counter() - t) * 1000)
    fused = rrf(dense, sparse, weights=RRF_WEIGHTS) if mode == 'hybrid' else (dense or sparse)
    films: dict[str, dict] = {}
    for cid, score in fused:
        row = con.execute('SELECT film_id, kind, text, source_url FROM kg_chunks WHERE id=?', (cid,)).fetchone()
        film = films.setdefault(row[0], {'film_id': row[0], 'score': score, 'chunks': []})
        if len(film['chunks']) < 2:
            film['chunks'].append({'chunk_id': cid, 'kind': row[1], 'text': row[2], 'source_url': row[3]})
        if len(films) >= k and row[0] not in films:
            break
    ranked = sorted(films.values(), key=lambda f: -f['score'])[:k]
    return {'films': ranked, 'timings': timings, 'dense_hits': len(dense), 'sparse_hits': len(sparse)}


_STOP = set('the and for with that this from was were are his her him she they them their who whom what which '
            'when where film movie malayalam about story plot one into then than has have had there been'.split())
