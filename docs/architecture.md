# Architecture and expansion plan

## Core model

| Entity or edge | Stored facts | Why it matters |
| --- | --- | --- |
| Film | Stable ID, title, release year, language, external IDs | Allows title and year filtering and cross source joins |
| Person | Stable ID, display name, spelling aliases | Resolves romanization and approximate name clues |
| Credit edge | Film ID, person ID, role, optional character, source | Distinguishes acting from directing, writing, producing, and technical work |
| Story document | Film ID, text, kind, source, license, retrieval time | Supports plot clues and auditable answers |
| Embedding | Document ID, model, text hash, vector | Semantic story similarity when the optional model is installed |

The retrieval order matters. The graph first narrows the candidate set using explicit constraints (year, people, role). Text and embeddings then rank those candidates by the remembered story. This prevents a semantically similar but wrong year or wrong cast film from winning purely on plot language.

## A production quality answer path

1. Parse the question into hard facts, soft clues, and uncertainty. Resolve Malayalam and Latin script aliases without forcing a match when several people are plausible.
2. Query the graph for matching films and preserve which edges satisfied each condition.
3. Retrieve plot passages for each candidate using both lexical and vector indexes.
4. Rank candidates using calibrated scores and inspect contradictions. A conflict in a hard fact should remove a candidate or be explicitly reported.
5. Generate a short answer from cited evidence, with a confidence level and an abstention option. Keep the complete evidence links available in the UI.

## Data ingestion roadmap

1. Build year by year Wikimedia imports and report counts, missing release dates, missing cast, and missing plots.
2. Add Wikimedia dumps for large backfills, with incremental API refreshes. Respect request limits and attribution.
3. Add a separately licensed metadata feed if the project needs coverage beyond Wikimedia. Do not infer permission to scrape from public page access.
4. Reconcile identities with stable external IDs and review uncertain title/year/name pairs. Keep each claim and its source before selecting a preferred value.
5. Split long plot text into scene or paragraph chunks and store source revisions, language, and offsets. Evaluate whether film level or chunk level embeddings better answer memory queries.

## Evaluation

Create a benchmark of remembered movie queries, including misspelled names, approximate years, Malayalam script clues, false cast memories, and ambiguous plots. Measure recall at 5 for candidate retrieval, top 1 accuracy, citation correctness, and abstention. Track performance by decade so newer films do not hide poor coverage of older cinema.

## Scaling decisions

SQLite is enough for the first personal research corpus and makes local development simple. At larger scale, add a job queue for ingestion and an ANN vector index (for example pgvector or Qdrant). Move graph edges to Neo4j only if relationship traversal or team workflows make the relational joins a bottleneck. Keep stable IDs and the JSON import contract so storage can change independently of the source adapters.
