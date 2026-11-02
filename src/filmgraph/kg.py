"""Entity resolution + canonical knowledge graph (the serving layer for GraphRAG).

Source tables keep every claim as imported. This module is non-destructive: it
derives `kg_*` tables with one node per real-world film/person, which both the
SQLite and Neo4j backends serve from.

Person resolution (blocking + matching, the usual ER recipe):
1. Union ids that external identifiers already link (Wikidata QID, IMDb nm, wiki slug).
2. Block by normalized name. Inside a block, merge anchored clusters (wiki/Wikidata/IMDb)
   that share at least one film: same name + overlapping filmography = same person.
3. Source-scoped `unresolved:` credits join the block's dominant anchored cluster when it
   has >= 5x the films of the runner-up; with no anchor they collapse into one name node.
Films: union linked identifiers, then exact (normalized title, year) duplicates.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from .db import normalize

# Raw source roles -> canonical role vocabulary used by NLU, Cypher and SQL templates.
ROLE_MAP = {
    'actor': 'actor', 'self': 'actor', 'director': 'director', 'second_unit_director': 'second_unit_director',
    'assistant_director': 'assistant_director', 'writer': 'writer', 'screenwriter': 'writer',
    'story_writer': 'writer', 'producer': 'producer', 'executive_producer': 'producer',
    'cinematographer': 'cinematographer', 'composer': 'music_director',
    'score_composer': 'music_director', 'editor': 'editor', 'lyricist': 'lyricist',
    'singer': 'singer', 'performer': 'singer', 'production_designer': 'production_designer',
    'art_director': 'production_designer', 'choreographer': 'choreographer',
    'costume_designer': 'costume_designer', 'sound_designer': 'sound_designer',
}
# Neo4j relationship type per canonical role (whitelist; never built from user text).
REL_TYPES = {
    'actor': 'ACTED_IN', 'director': 'DIRECTED', 'second_unit_director': 'DIRECTED_SECOND_UNIT', 'assistant_director': 'ASSISTED_DIRECTION',
    'writer': 'WROTE', 'producer': 'PRODUCED', 'cinematographer': 'SHOT',
    'music_director': 'SCORED', 'editor': 'EDITED', 'lyricist': 'WROTE_LYRICS_FOR',
    'singer': 'SANG_IN', 'production_designer': 'DESIGNED', 'choreographer': 'CHOREOGRAPHED',
    'costume_designer': 'COSTUMED', 'sound_designer': 'SOUND_DESIGNED',
}
SONG_ROLES = {'singer': 'singer', 'performer': 'singer', 'lyricist': 'lyricist',
              'composer': 'music_director', 'score_composer': 'music_director'}
FACT_KINDS = {'filming_location': 'location', 'technology_mention': 'technology',
              'film_format': 'technology', 'genre': 'genre', 'production_company': 'company',
              'production_companies': 'company'}
DOMINANCE = 5
# Table furniture that soundtrack/cast parsers occasionally emit as a "person".
NOT_PEOPLE = {'total length', 'length', 'instrumental', 'chorus', 'original songs', 'background music', 'various artists',
              'others', 'unknown', 'n a', 'na', 'tba', 'tbd', 'theme music', 'bgm', 'traditional', 'title song'}

KG_SCHEMA = """
DROP TABLE IF EXISTS kg_person_map; DROP TABLE IF EXISTS kg_film_map; DROP TABLE IF EXISTS kg_appearances;
DROP TABLE IF EXISTS kg_people; DROP TABLE IF EXISTS kg_films; DROP TABLE IF EXISTS kg_credits;
DROP TABLE IF EXISTS kg_songs; DROP TABLE IF EXISTS kg_song_credits; DROP TABLE IF EXISTS kg_facts;
DROP TABLE IF EXISTS kg_aliases;
CREATE TABLE kg_person_map(person_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, method TEXT NOT NULL);
CREATE TABLE kg_film_map(film_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, method TEXT NOT NULL);
CREATE TABLE kg_films(id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER, wikipedia_url TEXT,
  imdb_id TEXT, wikidata_id TEXT, source_ids INTEGER NOT NULL);
CREATE TABLE kg_people(id TEXT PRIMARY KEY, name TEXT NOT NULL, film_count INTEGER NOT NULL,
  primary_role TEXT, wikipedia_url TEXT, resolution TEXT NOT NULL, source_ids INTEGER NOT NULL);
CREATE TABLE kg_aliases(normalized TEXT NOT NULL, person_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY(normalized, person_id));
CREATE TABLE kg_credits(film_id TEXT NOT NULL, person_id TEXT NOT NULL, role TEXT NOT NULL,
  character_name TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '', PRIMARY KEY(film_id, person_id, role));
CREATE TABLE kg_songs(id TEXT PRIMARY KEY, film_id TEXT NOT NULL, title TEXT NOT NULL);
CREATE TABLE kg_song_credits(song_id TEXT NOT NULL, person_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY(song_id, person_id, role));
CREATE TABLE kg_facts(film_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(film_id, kind, value));
"""
KG_INDEXES = """
CREATE INDEX kg_credits_person ON kg_credits(person_id, role, film_id);
CREATE INDEX kg_credits_film ON kg_credits(film_id, role);
CREATE INDEX kg_song_credits_person ON kg_song_credits(person_id, role);
CREATE INDEX kg_songs_film ON kg_songs(film_id);
CREATE INDEX kg_facts_value ON kg_facts(kind, value);
CREATE INDEX kg_films_year ON kg_films(year);
CREATE INDEX kg_aliases_person ON kg_aliases(person_id);
"""


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Keep the most readable id as the root: wiki slug > Wikidata QID > imdb > other.
            self.parent[max(ra, rb, key=_id_rank)] = min(ra, rb, key=_id_rank)


def _id_rank(i: str) -> tuple:
    order = 0 if i.startswith('wiki:') else 1 if i[:1] == 'Q' and i[1:].isdigit() else 2 if i.startswith('imdb:') else 3
    return order, len(i), i


def _source_kind(url: str) -> str:
    return 'imdb' if 'imdb' in url else 'wikidata' if 'wikidata.org' in url else 'wikipedia' if 'wikipedia.org' in url else 'other'


def _title(film, enriched=frozenset()) -> str:
    """A wiki page id is authoritative: if a list row overwrote the title with an unrelated one
    (e.g. wiki:Drishyam titled 'Oru Indian Pranayakadha'), fall back to the page slug."""
    from difflib import SequenceMatcher
    from urllib.parse import unquote
    import re
    if not film['id'].startswith('wiki:') or film.get('wikipedia_url') not in enriched:
        return film['title']
    slug = re.sub(r'\s*\(.*?\)\s*$', '', unquote(film['id'][5:]).replace('_', ' '))
    return slug if SequenceMatcher(None, normalize(slug), normalize(film['title'])).ratio() < 0.5 else film['title']


def anchored(pid: str) -> bool:
    return not pid.startswith(('unresolved:', 'demo:'))


def _resolve_films(con) -> tuple[dict[str, str], dict[str, str]]:
    films = {r['id']: r for r in con.execute("SELECT * FROM films WHERE id NOT LIKE 'demo:%'")}
    uf, method = UnionFind(), {}
    for f in films:
        uf.find(f)
    for ident, fid in con.execute('SELECT identifier, film_id FROM film_identifiers'):
        if ident in films and fid in films and ident != fid:
            uf.union(ident, fid); method[ident] = method[fid] = 'identifier'
    blocks = defaultdict(list)
    for f in films.values():
        if f['year']:
            blocks[(normalize(f['title']), f['year'])].append(f['id'])
    for ids in blocks.values():
        for other in ids[1:]:
            if uf.find(other) != uf.find(ids[0]):
                uf.union(ids[0], other); method.setdefault(other, 'title_year'); method.setdefault(ids[0], 'title_year')
    return {f: uf.find(f) for f in films}, method


def _resolve_people(con, film_map) -> tuple[dict[str, str], dict[str, str]]:
    people = {r['id']: r['name'] for r in con.execute("SELECT id, name FROM people WHERE id NOT LIKE 'demo:%'")}
    films_of = defaultdict(set)
    for pid, fid in con.execute('SELECT person_id, film_id FROM credits'):
        if fid in film_map:
            films_of[pid].add(film_map[fid])
    for pid, fid in con.execute('SELECT c.person_id, s.film_id FROM song_credits c JOIN songs s ON s.id=c.song_id'):
        if fid in film_map:
            films_of[pid].add(film_map[fid])
    uf, method = UnionFind(), {}
    for p in people:
        uf.find(p)
    for ident, pid in con.execute('SELECT identifier, person_id FROM person_identifiers'):
        if ident in people and pid in people and ident != pid:
            uf.union(ident, pid); method[ident] = method[pid] = 'identifier'
    blocks = defaultdict(list)
    for pid, name in people.items():
        key = normalize(name)
        if key:
            blocks[key].append(pid)
    for ids in blocks.values():
        anchors = [p for p in ids if anchored(p)]
        # Anchored records with the same name that share a film are the same person.
        for i, a in enumerate(anchors):
            for b in anchors[i + 1:]:
                if uf.find(a) != uf.find(b) and films_of[a] & films_of[b]:
                    uf.union(a, b); method.setdefault(a, 'name+filmography'); method.setdefault(b, 'name+filmography')
        loose = [p for p in ids if not anchored(p)]
        if not loose:
            continue
        clusters = Counter()
        for a in anchors:
            clusters[uf.find(a)] = len(set().union(*(films_of[x] for x in anchors if uf.find(x) == uf.find(a))))
        ranked = clusters.most_common(2)
        if ranked and (len(ranked) == 1 or ranked[0][1] >= DOMINANCE * max(ranked[1][1], 1)):
            target, how = ranked[0][0], 'name->dominant_anchor'
        elif not ranked:
            target, how = loose[0], 'name_only'
        else:
            continue  # ambiguous name: several comparable anchored people; keep source-scoped
        for p in loose:
            uf.union(target, p); method[p] = how
    return {p: uf.find(p) for p in people}, method


def build_kg(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    film_map, film_method = _resolve_films(con)
    person_map, person_method = _resolve_people(con, film_map)
    con.executescript(KG_SCHEMA)
    con.executemany('INSERT INTO kg_film_map VALUES(?,?,?)',
                    [(f, c, film_method.get(f, 'self')) for f, c in film_map.items()])
    con.executemany('INSERT INTO kg_person_map VALUES(?,?,?)',
                    [(p, c, person_method.get(p, 'self')) for p, c in person_map.items()])

    films = defaultdict(list)
    for r in con.execute("SELECT * FROM films WHERE id NOT LIKE 'demo:%'"):
        films[film_map[r['id']]].append(dict(r))
    # Pages we actually parsed: their slug is evidence for the title.
    enriched = frozenset(r[0] for r in con.execute("SELECT item FROM import_jobs WHERE source='wikipedia_page' AND status='complete'"))
    wikidata = {}
    for fid, value in con.execute("SELECT film_id, value FROM facts WHERE kind='wikidata_id'"):
        if fid in film_map:
            wikidata.setdefault(film_map[fid], value)
    for cid, members in films.items():
        if cid[:1] == 'Q':
            wikidata.setdefault(cid, cid)
        best = min(members, key=lambda m: _id_rank(m['id']))
        pick = lambda key: next((m[key] for m in sorted(members, key=lambda m: _id_rank(m['id'])) if m[key]), None)
        con.execute('INSERT INTO kg_films VALUES(?,?,?,?,?,?,?)',
                    (cid, _title(best, enriched), pick('year'), pick('wikipedia_url'), pick('imdb_id'), wikidata.get(cid), len(members)))

    junk = {r[0] for r in con.execute('SELECT id, name FROM people') if normalize(r[1]) in NOT_PEOPLE or r[1].lstrip().startswith('&')}
    person_map = {p: c for p, c in person_map.items() if p not in junk and c not in junk}
    # Provenance per canonical edge: which independent sources assert it (wikipedia / wikidata / imdb).
    sources = defaultdict(set)
    for r in con.execute('SELECT film_id, person_id, role, source_url FROM credit_sources UNION ALL SELECT film_id, person_id, role, source_url FROM credits'):
        role = ROLE_MAP.get(r['role'])
        if role and r['film_id'] in film_map and r['person_id'] in person_map:
            sources[(film_map[r['film_id']], person_map[r['person_id']], role)].add(_source_kind(r['source_url']))
    seen = set()
    for r in con.execute('SELECT film_id, person_id, role, character_name FROM credits'):
        role = ROLE_MAP.get(r['role'])
        if not role or r['film_id'] not in film_map or r['person_id'] not in person_map:
            continue
        key = (film_map[r['film_id']], person_map[r['person_id']], role)
        if key not in seen:
            seen.add(key)
            con.execute('INSERT INTO kg_credits VALUES(?,?,?,?,?)', (*key, r['character_name'] or '', ','.join(sorted(sources[key]))))
    for r in con.execute('SELECT id, film_id, title FROM songs'):
        if r['film_id'] in film_map:
            con.execute('INSERT OR IGNORE INTO kg_songs VALUES(?,?,?)', (r['id'], film_map[r['film_id']], r['title']))
    for r in con.execute('SELECT song_id, person_id, role FROM song_credits'):
        role = SONG_ROLES.get(r['role'])
        if role and r['person_id'] in person_map:
            con.execute('INSERT OR IGNORE INTO kg_song_credits VALUES(?,?,?)', (r['song_id'], person_map[r['person_id']], role))
    for r in con.execute('SELECT film_id, kind, value FROM facts WHERE kind IN (%s)' % ','.join('?' * len(FACT_KINDS)), list(FACT_KINDS)):
        if r['film_id'] in film_map and 1 < len(r['value']) <= 80:
            con.execute('INSERT OR IGNORE INTO kg_facts VALUES(?,?,?)', (film_map[r['film_id']], FACT_KINDS[r['kind']], r['value'].strip()))

    # People: canonical name = the root record's name; count films across film + song credits.
    names = {r['id']: r['name'] for r in con.execute('SELECT id, name FROM people')}
    members = defaultdict(list)
    for p, c in person_map.items():
        members[c].append(p)
    stats = {r[0]: (r[1], r[2]) for r in con.execute(
        """SELECT person_id, count(DISTINCT film_id),
           (SELECT role FROM kg_credits k2 WHERE k2.person_id=k.person_id GROUP BY role ORDER BY count(*) DESC LIMIT 1)
           FROM (SELECT person_id, film_id FROM kg_credits UNION
                 SELECT c.person_id, s.film_id FROM kg_song_credits c JOIN kg_songs s ON s.id=c.song_id) k GROUP BY person_id""")}
    for cid, ids in members.items():
        if cid not in stats:
            continue
        resolution = 'anchored' if anchored(cid) else 'name_only'
        wiki = 'https://en.wikipedia.org/wiki/' + cid[5:] if cid.startswith('wiki:') else None
        # Display name = the spelling most source records agree on, not whichever id won the union.
        votes = Counter(names[i] for i in ids)
        display = max(votes, key=lambda n: (votes[n], n == names[cid]))
        con.execute('INSERT INTO kg_people VALUES(?,?,?,?,?,?,?)',
                    (cid, display, stats[cid][0], stats[cid][1], wiki, resolution, len(ids)))
    con.execute("""INSERT OR IGNORE INTO kg_aliases SELECT a.normalized, m.canonical_id, a.alias
                   FROM aliases a JOIN kg_person_map m ON m.person_id=a.person_id
                   JOIN kg_people p ON p.id=m.canonical_id WHERE length(a.normalized)>=3""")
    con.executemany('INSERT OR IGNORE INTO kg_aliases VALUES(?,?,?)',
                    [(normalize(name), pid, name) for pid, name in con.execute('SELECT id, name FROM kg_people')])
    con.executescript(KG_INDEXES)
    con.commit()
    count = lambda t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
    return {'films_raw': len(film_map), 'films': count('kg_films'), 'people_raw': len(person_map),
            'people': count('kg_people'), 'credits': count('kg_credits'), 'songs': count('kg_songs'),
            'song_credits': count('kg_song_credits'), 'facts': count('kg_facts'),
            'person_resolution': dict(Counter(person_method.get(p, 'self') for p in person_map)),
            'film_resolution': dict(Counter(film_method.get(f, 'self') for f in film_map))}
