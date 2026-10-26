# Evaluation report

Generated 2026-10-25 16:14. Gold: 50 structured questions, 29 remembered-plot questions (`evals/`).

| Configuration | Intent acc. | Entity F1 | Count exact | Crew answers | Film linking | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| Rules + classifier (no LLM) | 100% | 1.00 | 24/24 | 3/3 | 9/9 | 20.2 | 295.5 |

## Plot retrieval ablation

| Retriever | hit@1 | hit@5 | MRR | p50 ms |
|---|---|---|---|---|
| bm25 | 59% | 76% | 0.66 | 2.4 |
| vector | 66% | 86% | 0.73 | 11.0 |
| hybrid | 66% | 86% | 0.72 | 13.3 |

## Backend parity

SQLite mirror vs Neo4j identical film sets: **24/24**
