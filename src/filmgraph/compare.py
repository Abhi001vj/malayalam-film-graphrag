"""Typical-RAG baseline for the UI comparison panel.

A standard "chunk and retrieve" RAG system embeds or keyword-matches the raw question against text chunks and
hands the top passages to an LLM. This reproduces its retrieval step on the same Wikipedia chunks GraphRAG uses
(BM25, no entity linking, no graph, no aggregation), so reviewers can see what that pipeline would have to work with.
"""
from __future__ import annotations

import sqlite3

from .retrieval import bm25_search


def typical_rag(con: sqlite3.Connection, question: str, k: int = 5) -> dict:
    hits = bm25_search(con, question, k=k)
    passages = []
    for chunk_id, score in hits:
        row = con.execute("""SELECT c.text, c.source_url, c.kind, f.title, f.year FROM kg_chunks c
                             JOIN kg_films f ON f.id=c.film_id WHERE c.id=?""", (chunk_id,)).fetchone()
        if row:
            passages.append({'film': row[3], 'year': row[4], 'kind': row[2], 'score': round(score, 2),
                             'text': row[0][:320], 'url': row[1]})
    return {
        'method': 'BM25 over text chunks · raw question · top %d' % k,
        'passages': passages,
        'films': sorted({p['film'] for p in passages}),
    }
