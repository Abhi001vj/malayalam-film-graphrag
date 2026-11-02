"""Parameterised graph query templates with two interchangeable backends.

Neo4jStore runs Cypher against local Neo4j or AuraDB Free; SQLiteStore runs the same
templates as SQL over the kg_* mirror (used on Vercel / when Neo4j is off). Both return
(rows, query_text) so the trace can show exactly what ran. User text never reaches a
query string: only validated ids, whitelisted roles/relations and integers do.
"""
from __future__ import annotations

import os
import sqlite3
from collections import deque

from .kg import REL_TYPES
from . import settings

CORE_ROLES = ['actor', 'director', 'writer', 'producer', 'cinematographer', 'music_director', 'editor', 'lyricist']
SONG_RELS = {'singer': 'SANG', 'lyricist': 'WROTE_LYRICS', 'music_director': 'COMPOSED'}
FACT_LABELS = {'location': 'Place', 'technology': 'Technology', 'genre': 'Genre', 'company': 'Organization'}
TRIPLE_RELATIONS = {'REMAKE_OF', 'REMADE_AS', 'SEQUEL_TO', 'HAS_SEQUEL', 'BASED_ON', 'SHOT_AT', 'SET_IN',
                    'PRODUCED_BY', 'DISTRIBUTED_BY', 'WON_AWARD', 'NOMINATED_FOR', 'SCREENED_AT', 'INSPIRED_BY',
                    'DEBUTED_IN', 'MENTIONS'}


def _path_roles():
    return [r for r in settings.get('relations.path_roles') if r in REL_TYPES]


def _relations(relation):
    """Relations a triple query may return: the asked one if enabled, else every enabled one."""
    enabled = settings.get('relations.enabled')
    if relation in settings.RELATIONS:
        return [relation] if relation in enabled else []
    return [r for r in enabled if r != 'MENTIONS'] if relation is None else []


def _roles(roles):
    roles = [r for r in (roles or []) if r in REL_TYPES]
    return roles


class SQLiteStore:
    name = 'sqlite'

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def films_with(self, people: list[dict], year_from=None, year_to=None, facts: dict | None = None, limit=500):
        where, params = [], []
        for p in people:
            roles = _roles(p.get('roles'))
            sql = 'EXISTS(SELECT 1 FROM kg_credits c WHERE c.film_id=f.id AND c.person_id=?'
            params.append(p['id'])
            if roles:
                sql += ' AND c.role IN (%s)' % ','.join('?' * len(roles)); params += roles
            # singers/lyricists are often credited per song only
            sql += ') OR EXISTS(SELECT 1 FROM kg_song_credits sc JOIN kg_songs s ON s.id=sc.song_id WHERE s.film_id=f.id AND sc.person_id=?'
            params.append(p['id'])
            if roles:
                sql += ' AND sc.role IN (%s)' % ','.join('?' * len(roles)); params += roles
            where.append('(' + sql + '))')
        if year_from:
            where.append('f.year>=?'); params.append(int(year_from))
        if year_to:
            where.append('f.year<=?'); params.append(int(year_to))
        for kind, value in (facts or {}).items():
            where.append('EXISTS(SELECT 1 FROM kg_facts x WHERE x.film_id=f.id AND x.kind=? AND x.value LIKE ?)')
            params += [kind, '%' + value + '%']
        sql = ('SELECT f.id, f.title, f.year FROM kg_films f' + (' WHERE ' + ' AND '.join(where) if where else '')
               + ' ORDER BY f.year DESC, f.title LIMIT ?')
        rows = [dict(r) for r in self.con.execute(sql, [*params, limit])]
        return rows, sql

    def top_collaborators(self, person_id, other_roles=None, limit=15):
        roles = _roles(other_roles)
        sql = """SELECT p.id, p.name, count(DISTINCT b.film_id) films, group_concat(DISTINCT b.role) roles
                 FROM kg_credits a JOIN kg_credits b ON a.film_id=b.film_id AND b.person_id!=a.person_id
                 JOIN kg_people p ON p.id=b.person_id WHERE a.person_id=?"""
        params = [person_id]
        if roles:
            sql += ' AND b.role IN (%s)' % ','.join('?' * len(roles)); params += roles
        sql += ' GROUP BY p.id ORDER BY films DESC LIMIT ?'
        return [dict(r) for r in self.con.execute(sql, [*params, limit])], sql

    def film_credits(self, film_id, roles=None):
        roles = _roles(roles)
        sql = 'SELECT p.id, p.name, c.role, c.character_name FROM kg_credits c JOIN kg_people p ON p.id=c.person_id WHERE c.film_id=?'
        params = [film_id]
        if roles:
            sql += ' AND c.role IN (%s)' % ','.join('?' * len(roles)); params += roles
        rows = [dict(r) for r in self.con.execute(sql + ' ORDER BY c.role, p.film_count DESC', params)]
        if roles and not rows:  # music credits may exist only at song level
            sql2 = """SELECT DISTINCT p.id, p.name, sc.role, '' character_name FROM kg_song_credits sc JOIN kg_songs s ON s.id=sc.song_id
                      JOIN kg_people p ON p.id=sc.person_id WHERE s.film_id=? AND sc.role IN (%s)""" % ','.join('?' * len(roles))
            rows = [dict(r) for r in self.con.execute(sql2, [film_id, *roles])]
            sql += ' ;fallback: ' + sql2
        return rows, sql

    def songs(self, people: list[dict], film_id=None, limit=100):
        where, params = [], []
        for p in people:
            if p.get('film_roles'):  # "Mohanlal films": a credit on the film, not on the song
                where.append('EXISTS(SELECT 1 FROM kg_credits k WHERE k.film_id=s.film_id AND k.person_id=? AND k.role IN (%s))'
                             % ','.join('?' * len(p['film_roles'])))
                params += [p['id'], *p['film_roles']]
                continue
            roles = _roles(p.get('roles')) or ['singer', 'lyricist', 'music_director']
            sql = 'EXISTS(SELECT 1 FROM kg_song_credits c WHERE c.song_id=s.id AND c.person_id=? AND c.role IN (%s))' % ','.join('?' * len(roles))
            params += [p['id'], *roles]
            if 'music_director' in roles:  # composers are usually credited per film, not per song
                sql = f"({sql} OR EXISTS(SELECT 1 FROM kg_credits k WHERE k.film_id=s.film_id AND k.person_id=? AND k.role='music_director'))"
                params.append(p['id'])
            where.append(sql)
        if film_id:
            where.append('s.film_id=?'); params.append(film_id)
        sql = ('SELECT s.id, s.title, f.id film_id, f.title film, f.year FROM kg_songs s JOIN kg_films f ON f.id=s.film_id'
               + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY f.year DESC LIMIT ?')
        return [dict(r) for r in self.con.execute(sql, [*params, limit])], sql

    def shortest_path(self, a, b, max_hops=6):
        """Bidirectional BFS over the person-film bipartite graph (roles chosen in settings)."""
        roles = _path_roles()
        sql = f"BFS over kg_credits(person_id, film_id) WHERE role IN ({', '.join(roles)})"
        if a == b:
            return [a], sql
        def neighbours(node):
            col, other = ('person_id', 'film_id') if not self._is_film(node) else ('film_id', 'person_id')
            return [r[0] for r in self.con.execute(
                f'SELECT {other} FROM kg_credits WHERE {col}=? AND role IN (%s)' % ','.join('?' * len(roles)), [node, *roles])]
        parents = {a: None}; parents_b = {b: None}
        qa, qb = deque([a]), deque([b])
        for _ in range(max_hops * 2):
            for queue, mine, theirs in ((qa, parents, parents_b), (qb, parents_b, parents)):
                for _ in range(len(queue)):
                    node = queue.popleft()
                    for nxt in neighbours(node):
                        if nxt in mine:
                            continue
                        mine[nxt] = node
                        if nxt in theirs:
                            left, n = [], nxt
                            while n is not None:
                                left.append(n); n = parents[n]
                            right, n = [], parents_b[nxt]
                            while n is not None:
                                right.append(n); n = parents_b[n]
                            return left[::-1] + right, sql
                        queue.append(nxt)
            if not qa or not qb:
                break
        return [], sql

    def _is_film(self, node):
        return self.con.execute('SELECT 1 FROM kg_films WHERE id=?', (node,)).fetchone() is not None

    def related(self, relation=None, film_id=None, object_id=None, limit=100):
        relations = _relations(relation)
        if not relations:
            return [], '-- relation disabled in settings'
        where, params = ['t.relation IN (%s)' % ','.join('?' * len(relations))], list(relations)
        if film_id:
            where.append('t.film_id=?'); params.append(film_id)
        if object_id:
            where.append('(t.object_id=? OR t.subject_id=?)'); params += [object_id, object_id]
        sql = f"""SELECT t.subject_id, t.relation, t.object_id, t.film_id, f.title film, f.year, coalesce(e.title, t.object_id) object,
                  e.type object_type, t.evidence, t.method, t.confidence FROM kg_triples t JOIN kg_films f ON f.id=t.film_id
                  LEFT JOIN kg_entities e ON e.id=t.object_id WHERE {' AND '.join(where)}
                  ORDER BY t.confidence DESC, f.year DESC LIMIT ?"""
        return [dict(r) for r in self.con.execute(sql, [*params, limit])], sql


class Neo4jStore:
    name = 'neo4j'

    def __init__(self, driver, database=None):
        self.driver = driver
        self.database = database or settings.get('models.neo4j_database')

    def run(self, cypher, **params):
        records, _, _ = self.driver.execute_query(cypher, params, database_=self.database, routing_='r')
        return [r.data() for r in records]

    @staticmethod
    def _rel(roles):
        # No role -> any *credit* relationship; never extracted triples such as DEBUTED_IN or MENTIONS.
        roles = _roles(roles) or list(REL_TYPES)
        return ':' + '|'.join(REL_TYPES[r] for r in roles)

    def films_with(self, people, year_from=None, year_to=None, facts=None, limit=500):
        lines, params = ['MATCH (f:Film)'], {'limit': limit}
        conds = []
        for i, p in enumerate(people):
            params[f'p{i}'] = p['id']
            rel = self._rel(p.get('roles'))
            song_rels = [SONG_RELS[r] for r in (p.get('roles') or []) if r in SONG_RELS]
            song_rel = ':' + '|'.join(song_rels) if song_rels else ''
            alt = f' OR EXISTS {{ MATCH (:Person {{id: $p{i}}})-[{song_rel}]->(:Song)-[:IN_FILM]->(f) }}' if (not p.get('roles') or song_rel) else ''
            conds.append(f'(EXISTS {{ MATCH (:Person {{id: $p{i}}})-[{rel}]->(f) }}{alt})')
        if year_from:
            conds.append('f.year >= $year_from'); params['year_from'] = int(year_from)
        if year_to:
            conds.append('f.year <= $year_to'); params['year_to'] = int(year_to)
        for kind, value in (facts or {}).items():
            label = FACT_LABELS[kind]
            conds.append(f'EXISTS {{ MATCH (f)--(x:{label}) WHERE toLower(x.name) CONTAINS toLower(${kind}) }}')
            params[kind] = value
        if conds:
            lines.append('WHERE ' + '\n  AND '.join(conds))
        lines.append('RETURN f.id AS id, f.title AS title, f.year AS year ORDER BY coalesce(year, 0) DESC, title LIMIT $limit')
        cypher = '\n'.join(lines)
        return self.run(cypher, **params), cypher

    def top_collaborators(self, person_id, other_roles=None, limit=15):
        cypher = f"""MATCH (a:Person {{id: $id}})-[{self._rel(None)}]->(f:Film)<-[r{self._rel(other_roles)}]-(p:Person)
WHERE p <> a
RETURN p.id AS id, p.name AS name, count(DISTINCT f) AS films, collect(DISTINCT r.role) AS roles
ORDER BY films DESC LIMIT $limit"""
        rows = self.run(cypher, id=person_id, limit=limit)
        for r in rows:
            r['roles'] = ','.join(x for x in r['roles'] if x)
        return rows, cypher

    def film_credits(self, film_id, roles=None):
        cypher = f"""MATCH (p:Person)-[r{self._rel(roles)}]->(f:Film {{id: $id}})
RETURN p.id AS id, p.name AS name, r.role AS role, coalesce(r.character, '') AS character_name
ORDER BY role, p.film_count DESC"""
        return self.run(cypher, id=film_id), cypher

    def songs(self, people, film_id=None, limit=100):
        song_types = SONG_RELS
        lines, params = ['MATCH (s:Song)-[:IN_FILM]->(f:Film)'], {'limit': limit}
        conds = []
        for i, p in enumerate(people):
            params[f'p{i}'] = p['id']
            if p.get('film_roles'):  # "Mohanlal films": a credit on the film, not on the song
                conds.append(f'EXISTS {{ MATCH (:Person {{id: $p{i}}})-[{self._rel(p["film_roles"])}]->(f) }}')
                continue
            roles = [r for r in (p.get('roles') or song_types) if r in song_types]
            rels = '|'.join(song_types[r] for r in roles)
            cond = f'EXISTS {{ MATCH (:Person {{id: $p{i}}})-[:{rels}]->(s) }}'
            if 'music_director' in roles:  # composers are usually credited per film, not per song
                cond = f'({cond} OR EXISTS {{ MATCH (:Person {{id: $p{i}}})-[:SCORED]->(f) }})'
            conds.append(cond); params[f'p{i}'] = p['id']
        if film_id:
            conds.append('f.id = $film'); params['film'] = film_id
        if conds:
            lines.append('WHERE ' + ' AND '.join(conds))
        lines.append('RETURN s.id AS id, s.title AS title, f.id AS film_id, f.title AS film, f.year AS year ORDER BY coalesce(year, 0) DESC LIMIT $limit')
        cypher = '\n'.join(lines)
        return self.run(cypher, **params), cypher

    def shortest_path(self, a, b, max_hops=6):
        rels = '|'.join(REL_TYPES[r] for r in _path_roles())
        cypher = f"""MATCH (a:Person {{id: $a}}), (b:Person {{id: $b}})
MATCH p = shortestPath((a)-[:{rels}*..{max_hops * 2}]-(b))
RETURN [n IN nodes(p) | n.id] AS path"""
        rows = self.run(cypher, a=a, b=b)
        return (rows[0]['path'] if rows else []), cypher

    def related(self, relation=None, film_id=None, object_id=None, limit=100):
        relations = _relations(relation)
        if not relations:
            return [], '// relation disabled in settings'
        rel = ':' + '|'.join(relations)  # whitelisted names only
        conds = []
        if film_id:
            conds.append('r.film_id = $film')
        if object_id:
            conds.append('(o.id = $obj OR s.id = $obj)')
        cypher = f"""MATCH (s)-[r{rel}]->(o) WHERE r.method IS NOT NULL{''.join(' AND ' + c for c in conds)}
MATCH (f:Film {{id: r.film_id}})
RETURN s.id AS subject_id, type(r) AS relation, o.id AS object_id, r.film_id AS film_id, f.title AS film, f.year AS year,
       coalesce(o.name, o.title) AS object, head([l IN labels(o) WHERE l <> 'Entity']) AS object_type,
       r.evidence AS evidence, r.method AS method, r.confidence AS confidence
ORDER BY confidence DESC, coalesce(year, 0) DESC LIMIT $limit"""
        return self.run(cypher, film=film_id, obj=object_id, limit=limit), cypher


def open_store(con: sqlite3.Connection, prefer: str | None = None):
    """Neo4j when configured and reachable, otherwise the SQLite mirror."""
    prefer = prefer or settings.get('models.graph_backend')
    if prefer in ('auto', 'neo4j') and settings.get('models.neo4j_uri'):
        try:
            store = Neo4jStore(_driver())
            if _LOADED.get('ok') is None:  # an empty/unloaded Neo4j must never answer
                _LOADED['ok'] = bool(store.run('MATCH (f:Film) RETURN count(f) > 0 AS ok')[0]['ok'])
            if _LOADED['ok']:
                return store
            if prefer == 'neo4j':
                raise RuntimeError('Neo4j is reachable but the graph is not loaded; run `filmgraph load-neo4j`')
        except Exception:
            _LOADED.pop('ok', None)
            if prefer == 'neo4j':
                raise
    return SQLiteStore(con)


_DRIVER: dict = {}
_LOADED: dict = {}


def _driver():
    key = (settings.get('models.neo4j_uri'), settings.get('models.neo4j_username'))
    if _DRIVER.get('key') != key:
        from neo4j import GraphDatabase
        reset_driver()
        _DRIVER.update(key=key, driver=GraphDatabase.driver(key[0], auth=(key[1], os.getenv('NEO4J_PASSWORD', ''))))
    return _DRIVER['driver']


def reset_driver() -> None:
    """Called when settings change: reconnect and re-check that the graph is loaded."""
    old = _DRIVER.pop('driver', None)
    _DRIVER.clear(); _LOADED.clear()
    if old is not None:
        old.close()
