"""GraphRAG communities (Microsoft GraphRAG "global search" pattern, small-scale).

1. Project the bipartite credit graph to a weighted person-person collaboration graph
   (weight = shared films, core creative roles, people with >= MIN_FILMS films).
2. Louvain community detection (networkx, seeded => reproducible).
3. Each community gets a deterministic fact sheet (members, roles, era, signature films)
   and, when a local LLM is available, a short natural-language report written ONLY from that
   fact sheet. Reports are embedded so global questions retrieve the relevant communities.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations

import numpy as np

from . import llm

MIN_FILMS = 8
ROLES = ('actor', 'director', 'writer', 'music_director', 'cinematographer', 'editor', 'producer', 'lyricist')


def detect(con: sqlite3.Connection, resolution: float = 2.5, seed: int = 7) -> dict:
    import networkx as nx  # build-time only; serving never imports it
    people = {r[0] for r in con.execute('SELECT id FROM kg_people WHERE film_count >= ?', (MIN_FILMS,))}
    by_film = defaultdict(set)
    for film, person in con.execute('SELECT film_id, person_id FROM kg_credits WHERE role IN (%s)' % ','.join('?' * len(ROLES)), ROLES):
        if person in people:
            by_film[film].add(person)
    weights = Counter()
    for members in by_film.values():
        if len(members) <= 40:  # giant ensemble casts add noise, not signal
            for a, b in combinations(sorted(members), 2):
                weights[(a, b)] += 1
    graph = nx.Graph()
    graph.add_weighted_edges_from((a, b, w) for (a, b), w in weights.items() if w >= 2)
    parts = nx.community.louvain_communities(graph, weight='weight', resolution=resolution, seed=seed)
    parts = sorted((p for p in parts if len(p) >= 5), key=len, reverse=True)
    con.executescript("""DROP TABLE IF EXISTS kg_communities; DROP TABLE IF EXISTS kg_community_members;
      CREATE TABLE kg_communities(id TEXT PRIMARY KEY, size INTEGER, title TEXT, facts TEXT, summary TEXT, method TEXT);
      CREATE TABLE kg_community_members(person_id TEXT PRIMARY KEY, community_id TEXT, centrality REAL);""")
    for index, members in enumerate(parts):
        cid = f'c{index:03d}'
        sub = graph.subgraph(members)
        centrality = {n: sum(d['weight'] for _, _, d in sub.edges(n, data=True)) for n in members}
        con.executemany('INSERT INTO kg_community_members VALUES(?,?,?)', [(p, cid, centrality[p]) for p in members])
        facts = fact_sheet(con, members, centrality)
        title = ' · '.join(m['name'] for m in facts['core_members'][:3])
        con.execute('INSERT INTO kg_communities VALUES(?,?,?,?,?,?)',
                    (cid, len(members), title, json.dumps(facts, ensure_ascii=False), template_summary(facts), 'template'))
    con.commit()
    return {'people_in_graph': graph.number_of_nodes(), 'edges': graph.number_of_edges(), 'communities': len(parts),
            'modularity': round(nx.community.modularity(graph, parts + [set(graph) - set().union(*parts)], weight='weight'), 3)}


def fact_sheet(con, members, centrality) -> dict:
    ids = list(members)
    marks = ','.join('?' * len(ids))
    top = sorted(ids, key=lambda p: -centrality[p])[:12]
    names = {r[0]: (r[1], r[2]) for r in con.execute(f'SELECT id, name, primary_role FROM kg_people WHERE id IN ({marks})', ids)}
    films = con.execute(f"""SELECT f.id, f.title, f.year, count(DISTINCT c.person_id) n FROM kg_credits c JOIN kg_films f ON f.id=c.film_id
        WHERE c.person_id IN ({marks}) GROUP BY f.id ORDER BY n DESC, f.year LIMIT 8""", ids).fetchall()
    years = [r[0] for r in con.execute(f'SELECT f.year FROM kg_credits c JOIN kg_films f ON f.id=c.film_id WHERE c.person_id IN ({marks}) AND f.year IS NOT NULL', ids)]
    roles = Counter(names[p][1] for p in ids if p in names)
    return {'size': len(ids),
            'core_members': [{'id': p, 'name': names[p][0], 'role': names[p][1]} for p in top if p in names],
            'roles': dict(roles.most_common(6)),
            'active_years': [int(np.percentile(years, 10)), int(np.percentile(years, 90))] if years else None,
            'peak_decade': (Counter(y // 10 * 10 for y in years).most_common(1)[0][0] if years else None),
            'signature_films': [{'id': r[0], 'title': r[1], 'year': r[2], 'members': r[3]} for r in films]}


def template_summary(f: dict) -> str:
    core = ', '.join(f"{m['name']} ({m['role'].replace('_', ' ')})" for m in f['core_members'][:5])
    years = f"{f['active_years'][0]}–{f['active_years'][1]}" if f['active_years'] else 'unknown years'
    films = ', '.join(f"{x['title']} ({x['year']})" for x in f['signature_films'][:4])
    return (f"A collaboration circle of {f['size']} people, mostly active {years} (peak {f['peak_decade']}s). "
            f"Core: {core}. Films where most of them overlap: {films}.")


PROMPT = """Write a 3-sentence report on this collaboration community in Malayalam cinema.
Use ONLY the facts below; do not add names, films, years or claims not present. Mention the era, the
key people and their roles, and the signature films.
FACTS: {facts}"""


def summarise(con: sqlite3.Connection, top: int = 40, log=print) -> int:
    """LLM community reports for the largest communities (others keep the template)."""
    done = 0
    for cid, facts in con.execute("SELECT id, facts FROM kg_communities WHERE method='template' ORDER BY size DESC LIMIT ?", (top,)).fetchall():
        try:
            text, _ = llm.chat([{'role': 'user', 'content': PROMPT.format(facts=facts)}], max_tokens=220)
        except llm.LLMUnavailable as exc:
            log(f'LLM unavailable, keeping template summaries: {exc}'); break
        con.execute("UPDATE kg_communities SET summary=?, method='llm' WHERE id=?", (text, cid)); con.commit()
        done += 1
        log(f'Community {cid} summarised')
    return done


def search_communities(con: sqlite3.Connection, question: str, k: int = 4) -> list[dict]:
    rows = [dict(r) for r in con.execute('SELECT id, size, title, summary, facts FROM kg_communities')]
    if not rows:
        return []
    try:
        vectors = np.asarray(llm.embed(['title: none | text: ' + r['summary'] for r in rows] + ['task: search result | query: ' + question]), dtype=np.float32)
        scores = vectors[:-1] @ vectors[-1]
    except llm.LLMUnavailable:
        words = set(question.lower().split())
        scores = np.array([len(words & set(r['summary'].lower().split())) for r in rows], dtype=float)
    order = np.argsort(-scores)[:k]
    return [{**rows[i], 'facts': json.loads(rows[i]['facts']), 'score': float(scores[i])} for i in order]
