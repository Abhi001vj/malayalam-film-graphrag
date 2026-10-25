"""Load the canonical KG into Neo4j (local Docker or AuraDB Free) with batched UNWIND.

Graph model
  (:Person)-[:ACTED_IN|DIRECTED|WROTE|SCORED|SHOT|EDITED|PRODUCED|...{role}]->(:Film)
  (:Person)-[:SANG|COMPOSED|WROTE_LYRICS]->(:Song)-[:IN_FILM]->(:Film)
  (:Film)-[:SHOT_AT|SET_IN|BASED_ON|REMAKE_OF|PRODUCED_BY|WON_AWARD|...{evidence,method,confidence}]->(:Entity:<Type>)
  (:Film)-[:HAS_GENRE]->(:Genre), (:Film)-[:USES]->(:Technology), (:Film)-[:FILMED_IN]->(:Place)
  (:Chunk {text, embedding})-[:PART_OF]->(:Film)          lexical graph for vector/fulltext search
  (:Person)-[:IN_COMMUNITY]->(:Community {title, summary}) GraphRAG communities
AuraDB Free caps at 200k nodes / 400k relationships; `load` reports the totals.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from .kg import REL_TYPES

BATCH = 5000
CONSTRAINTS = [
    'CREATE CONSTRAINT film_id IF NOT EXISTS FOR (n:Film) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT person_id IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT song_id IF NOT EXISTS FOR (n:Song) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (n:Chunk) REQUIRE n.id IS UNIQUE',
    'CREATE CONSTRAINT community_id IF NOT EXISTS FOR (n:Community) REQUIRE n.id IS UNIQUE',
    'CREATE INDEX film_year IF NOT EXISTS FOR (n:Film) ON (n.year)',
    'CREATE FULLTEXT INDEX names IF NOT EXISTS FOR (n:Person|Film|Entity) ON EACH [n.name, n.title]',
    'CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]',
    """CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS FOR (n:Chunk) ON (n.embedding)
       OPTIONS {indexConfig: {`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}""",
]


def _batches(rows):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def load(con: sqlite3.Connection, driver, database='neo4j', wipe=True, log=print) -> dict:
    con.row_factory = sqlite3.Row
    run = lambda q, **p: driver.execute_query(q, p, database_=database)
    if wipe:
        log('Clearing database')
        with driver.session(database=database) as session:  # IN TRANSACTIONS needs an auto-commit transaction
            session.run('MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS').consume()
    for statement in CONSTRAINTS:
        run(statement)

    def push(query, rows, label):
        for part in _batches(rows):
            run(query, rows=part)
        log(f'{label}: {len(rows)}')

    push('UNWIND $rows AS r MERGE (f:Film {id: r.id}) SET f += r',
         [dict(r) for r in con.execute('SELECT id, title, year, wikipedia_url, imdb_id, wikidata_id FROM kg_films')], 'films')
    push('UNWIND $rows AS r MERGE (p:Person {id: r.id}) SET p += r',
         [dict(r) for r in con.execute('SELECT id, name, film_count, primary_role, wikipedia_url, resolution FROM kg_people')], 'people')
    for role, rel in REL_TYPES.items():
        rows = [dict(r) for r in con.execute('SELECT person_id p, film_id f, character_name c FROM kg_credits WHERE role=?', (role,))]
        if rows:
            push(f"""UNWIND $rows AS r MATCH (p:Person {{id: r.p}}), (f:Film {{id: r.f}})
                     MERGE (p)-[x:{rel}]->(f) SET x.role = '{role}', x.character = r.c""", rows, rel)
    push('UNWIND $rows AS r MATCH (f:Film {id: r.film_id}) MERGE (s:Song {id: r.id}) SET s.title = r.title MERGE (s)-[:IN_FILM]->(f)',
         [dict(r) for r in con.execute('SELECT id, film_id, title FROM kg_songs')], 'songs')
    for role, rel in {'singer': 'SANG', 'lyricist': 'WROTE_LYRICS', 'music_director': 'COMPOSED'}.items():
        push(f'UNWIND $rows AS r MATCH (p:Person {{id: r.p}}), (s:Song {{id: r.s}}) MERGE (p)-[:{rel}]->(s)',
             [dict(r) for r in con.execute('SELECT person_id p, song_id s FROM kg_song_credits WHERE role=?', (role,))], rel)
    for kind, (label, rel) in {'location': ('Place', 'FILMED_IN'), 'technology': ('Technology', 'USES'),
                               'genre': ('Genre', 'HAS_GENRE'), 'company': ('Organization', 'PRODUCED_BY')}.items():
        push(f"""UNWIND $rows AS r MATCH (f:Film {{id: r.f}}) MERGE (x:Entity:{label} {{id: '{kind}:' + toLower(r.v)}})
                 ON CREATE SET x.name = r.v MERGE (f)-[e:{rel}]->(x) SET e.method = 'infobox', e.film_id = r.f, e.confidence = 0.9""",
             [dict(r) for r in con.execute('SELECT film_id f, value v FROM kg_facts WHERE kind=?', (kind,))], kind)

    # Link-anchored triples: objects that are KG people/films attach to those nodes; others become typed entities.
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_triples'").fetchone():
        entities = [dict(r) for r in con.execute(
            "SELECT id, title name, type, qid, description FROM kg_entities WHERE kg_id IS NULL AND type IN ('Place','Organization','Work','Award','Event','Film','Concept','Person')")]
        for label in {e['type'] for e in entities}:
            push(f'UNWIND $rows AS r MERGE (x:Entity {{id: r.id}}) SET x += r, x:{label}',
                 [e for e in entities if e['type'] == label], f'entities:{label}')
        kg_ids = dict(con.execute('SELECT id, kg_id FROM kg_entities WHERE kg_id IS NOT NULL'))
        rows = [dict(r) for r in con.execute('SELECT * FROM kg_triples')]
        for r in rows:
            r['subject_id'] = kg_ids.get(r['subject_id'], r['subject_id'])
            r['object_id'] = kg_ids.get(r['object_id'], r['object_id'])
        for relation in sorted({r['relation'] for r in rows}):
            push(f"""UNWIND $rows AS r MATCH (s {{id: r.subject_id}}) MATCH (o {{id: r.object_id}})
                     WHERE (s:Film OR s:Person OR s:Entity) AND (o:Film OR o:Person OR o:Entity)
                     MERGE (s)-[x:{relation} {{method: r.method}}]->(o)
                     SET x.evidence = r.evidence, x.source_url = r.source_url, x.confidence = r.confidence, x.film_id = r.film_id""",
                 [r for r in rows if r['relation'] == relation], f'triples:{relation}')

    ids_path, vec_path = Path('data/vectors/chunk_ids.npy'), Path('data/vectors/chunk_vectors.npy')
    if vec_path.exists():
        vectors = dict(zip(np.load(ids_path).tolist(), np.load(vec_path)))
        rows = [{'id': r['id'], 'film_id': r['film_id'], 'kind': r['kind'], 'text': r['text'],
                 'source_url': r['source_url'], 'embedding': vectors[r['id']].tolist()}
                for r in con.execute('SELECT * FROM kg_chunks') if r['id'] in vectors]
        for part in _batches(rows):
            for small in (part[i:i + 1000] for i in range(0, len(part), 1000)):
                run("""UNWIND $rows AS r MATCH (f:Film {id: r.film_id}) MERGE (c:Chunk {id: r.id})
                       SET c.text = r.text, c.kind = r.kind, c.source_url = r.source_url
                       WITH c, f, r CALL db.create.setNodeVectorProperty(c, 'embedding', r.embedding)
                       MERGE (c)-[:PART_OF]->(f)""", rows=small)
        log(f'chunks: {len(rows)}')
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_communities'").fetchone():
        push('UNWIND $rows AS r MERGE (c:Community {id: r.id}) SET c.title = r.title, c.summary = r.summary, c.size = r.size',
             [dict(r) for r in con.execute('SELECT id, title, summary, size FROM kg_communities')], 'communities')
        push('UNWIND $rows AS r MATCH (p:Person {id: r.p}), (c:Community {id: r.c}) MERGE (p)-[:IN_COMMUNITY]->(c)',
             [dict(r) for r in con.execute('SELECT person_id p, community_id c FROM kg_community_members')], 'memberships')
    records, _, _ = driver.execute_query('MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() RETURN nodes, count(r) AS rels', database_=database)
    totals = records[0].data()
    totals['aura_free_fits'] = totals['nodes'] <= 200_000 and totals['rels'] <= 400_000
    return totals
