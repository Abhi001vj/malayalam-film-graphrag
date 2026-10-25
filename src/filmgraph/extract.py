"""Triple extraction from film pages: link-anchored, rules first, LLM second.

Credits already arrive as (person)-[ROLE]->(film) triples from infoboxes, tables and
Wikidata. This module mines the prose. Wikipedia hyperlinks give free, exact entity
linking, so extraction only has to decide the *relation*:

  stage A  mentions   every linked anchor in lead/plot/production prose + infobox rows
  stage B  typing     link target -> KG person/film, else Wikidata P31/description type
  stage C  rules      cue patterns ("remake of", "based on", "shot in", "won the") -> typed triple
  stage D  LLM        local model labels remaining film<->entity pairs from a closed relation set

Every triple keeps sentence evidence, source URL, method and confidence.
"""
from __future__ import annotations

import re
import sqlite3
import time
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup

from . import llm
from .catalogue import article_link, clean_text, section_content
from .http import Client

PROSE_SECTIONS = ('lead', 'plot', 'plot summary', 'synopsis', 'production', 'development', 'filming',
                  'casting', 'release', 'reception', 'release and reception', 'accolades', 'awards',
                  'remakes', 'sequel', 'legacy', 'music', 'soundtrack')
INFOBOX = {'based on': 'BASED_ON', 'production company': 'PRODUCED_BY', 'production companies': 'PRODUCED_BY',
           'distributed by': 'DISTRIBUTED_BY'}
RELATIONS = ['REMAKE_OF', 'REMADE_AS', 'SEQUEL_TO', 'HAS_SEQUEL', 'BASED_ON', 'SHOT_AT', 'SET_IN',
             'PRODUCED_BY', 'DISTRIBUTED_BY', 'WON_AWARD', 'NOMINATED_FOR', 'SCREENED_AT', 'DEBUT_OF',
             'INSPIRED_BY', 'REFERENCES', 'MENTIONS']
# cue regex (must appear shortly before the anchor) -> (relation, allowed object types)
CUES = [
    (r'\bremake of\b|\bremade from\b', 'REMAKE_OF', {'Film'}),
    (r'\bremade (?:in|into|as)\b|\bremake[sd]? in\b', 'REMADE_AS', {'Film'}),
    (r'\bsequel (?:to|of)\b', 'SEQUEL_TO', {'Film'}),
    (r'\b(?:sequel|followed by)\b', 'HAS_SEQUEL', {'Film'}),
    (r'\bbased on\b|\badapt(?:ation|ed) (?:of|from)\b', 'BASED_ON', {'Work', 'Film', 'Person'}),
    (r'\b(?:shot|filmed|filming|shooting|location)s? (?:was |were )?(?:in|at|across|around)\b', 'SHOT_AT', {'Place'}),
    (r'\bset (?:in|at|against)\b', 'SET_IN', {'Place'}),
    (r'\bproduced by\b|\bbanner\b|\bproduction (?:house|company)\b', 'PRODUCED_BY', {'Organization'}),
    (r'\bdistribut(?:ed|ion) (?:by|rights)\b', 'DISTRIBUTED_BY', {'Organization'}),
    (r'\b(?:won|winning|received|bagged|awarded)\b', 'WON_AWARD', {'Award'}),
    (r'\bnominat', 'NOMINATED_FOR', {'Award'}),
    (r'\bscreened at\b|\bpremiered at\b|\bfestival\b', 'SCREENED_AT', {'Event', 'Award'}),
    (r'\bdebut\b', 'DEBUT_OF', {'Person'}),
    (r'\binspired by\b', 'INSPIRED_BY', {'Film', 'Work', 'Person', 'Event'}),
]
TYPE_HINTS = [
    ('Film', r'\b(?:\d{4} )?(?:[A-Z][a-z]+ )?(?:language )?film\b|\bfilm by\b|\bmovie\b'),
    ('Work', r'\bnovel\b|\bbook\b|\bshort story\b|\bplay\b|\bpoem\b|\bnovella\b|\bmyth\b|\bepic\b|\bcomic\b|\btelevision series\b'),
    ('Award', r'\baward\b|\bprize\b|\bhonou?r\b|\bpuraskaram\b'),
    ('Event', r'\bfestival\b|\bceremony\b|\bwar\b|\bflood\b|\bmovement\b|\bincident\b|\briot\b|\bmassacre\b'),
    ('Organization', r'\bcompany\b|\bstudio\b|\bbanner\b|\bdistributor\b|\bchannel\b|\bnetwork\b|\bstreaming\b|\borgani[sz]ation\b|\bparty\b|\bbroadcaster\b|\bnewspaper\b|\bmagazine\b'),
    ('Place', r'\bcity\b|\btown\b|\bvillage\b|\bdistrict\b|\bstate\b|\bcountry\b|\bhill station\b|\bbeach\b|\briver\b|\blake\b|\btemple\b|\bfort\b|\bpalace\b|\bisland\b|\bmunicipality\b|\bregion\b|\bneighbourhood\b|\bsuburb\b|\bcapital\b|\bwaterfall\b|\bdam\b|\bforest\b|\bnational park\b|\bstation\b|\bcave\b|\bchurch\b|\bmosque\b'),
    ('Person', r'\bactor\b|\bactress\b|\bsinger\b|\bdirector\b|\bwriter\b|\bpoet\b|\bcomposer\b|\bpolitician\b|\bnovelist\b|\bproducer\b|\bcinematographer\b'),
]
FILM_TO_FILM = {'REMAKE_OF', 'REMADE_AS', 'SEQUEL_TO', 'HAS_SEQUEL', 'INSPIRED_BY'}
SENTENCE = re.compile(r'(?<=[a-z0-9\)"\'][.!?])\s+(?=[A-Z"\'])')

SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_mentions(film_id TEXT NOT NULL, section TEXT NOT NULL, sentence TEXT NOT NULL,
  anchor TEXT NOT NULL, target TEXT NOT NULL, offset INTEGER NOT NULL, source_url TEXT NOT NULL,
  PRIMARY KEY(film_id, target, sentence));
CREATE TABLE IF NOT EXISTS kg_entities(id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL,
  qid TEXT, description TEXT, kg_id TEXT);
CREATE TABLE IF NOT EXISTS kg_triples(subject_id TEXT NOT NULL, relation TEXT NOT NULL, object_id TEXT NOT NULL,
  film_id TEXT NOT NULL, evidence TEXT NOT NULL, source_url TEXT NOT NULL, method TEXT NOT NULL,
  confidence REAL NOT NULL, PRIMARY KEY(subject_id, relation, object_id, method));
CREATE INDEX IF NOT EXISTS kg_triples_object ON kg_triples(object_id, relation);
CREATE INDEX IF NOT EXISTS kg_triples_film ON kg_triples(film_id);
CREATE TABLE IF NOT EXISTS kg_llm_done(film_id TEXT NOT NULL, sentence_hash TEXT NOT NULL, PRIMARY KEY(film_id, sentence_hash));
"""


def _wiki_id(url: str) -> str:
    return 'wiki:' + unquote(urlparse(url).path.split('/wiki/', 1)[-1]).replace(' ', '_')


def _anchors(node) -> list[tuple[str, str]]:
    out = []
    for a in node.select('a[href]'):
        url = article_link(a)
        text = clean_text(a)
        if url and text and len(text) > 1 and not text.isdigit():
            out.append((text, _wiki_id(url)))
    return out


def mentions_from_page(html: bytes, film_id: str, url: str) -> tuple[list[tuple], list[tuple]]:
    soup = BeautifulSoup(html, 'html.parser')
    for noise in soup.select('sup.reference, .mw-editsection, script, style, .navbox, .reflist, .hatnote'):
        noise.decompose()
    infobox_rows = []
    infobox = soup.select_one('table.infobox')
    if infobox:
        for tr in infobox.select('tr'):
            th, td = tr.find('th'), tr.find('td')
            relation = INFOBOX.get(clean_text(th).casefold()) if th and td else None
            if relation:
                for text, target in _anchors(td):
                    infobox_rows.append((film_id, relation, target, text, f'Infobox: {clean_text(th)} = {clean_text(td)}'[:400], url))
        infobox.decompose()
    mentions = []
    for section, nodes in section_content(soup).items():
        if not section.startswith(PROSE_SECTIONS):
            continue
        for node in nodes:
            if node.name == 'table':
                continue
            text = clean_text(node)
            sentences = SENTENCE.split(text) if node.name == 'p' else [clean_text(li) for li in node.find_all('li')]
            cursor = 0
            for anchor, target in _anchors(node):
                for i in range(cursor, len(sentences)):
                    pos = sentences[i].find(anchor)
                    if pos >= 0:
                        cursor = i
                        mentions.append((film_id, section, sentences[i][:600], anchor, target, pos, url))
                        break
    return mentions, infobox_rows


def extract_mentions(con: sqlite3.Connection, client: Client | None = None, log=print) -> int:
    con.executescript(SCHEMA)
    client = client or Client()
    films = con.execute('SELECT id, wikipedia_url FROM kg_films WHERE wikipedia_url IS NOT NULL').fetchall()
    con.execute('DELETE FROM kg_mentions'); con.execute("DELETE FROM kg_triples WHERE method='infobox_link'")
    for n, (film_id, url) in enumerate(films, 1):
        try:
            html = client.get(url)  # cache hit for every page enriched earlier; no re-crawl
        except Exception:
            continue
        mentions, infobox = mentions_from_page(html, film_id, url)
        con.executemany('INSERT OR IGNORE INTO kg_mentions VALUES(?,?,?,?,?,?,?)', mentions)
        con.executemany("INSERT OR IGNORE INTO kg_triples VALUES(?,?,?,?,?,?,'infobox_link',0.95)",
                        [(f, rel, target, f, evidence, src) for f, rel, target, _, evidence, src in infobox])
        if n % 500 == 0:
            con.commit(); log(f'Mentions: {n}/{len(films)} pages')
    con.commit()
    return con.execute('SELECT count(*) FROM kg_mentions').fetchone()[0]


def type_entities(con: sqlite3.Connection, client: Client | None = None, log=print) -> dict:
    """Stage B: KG lookup first (free), then batched Wikidata typing for the rest."""
    con.executescript(SCHEMA)
    client = client or Client()
    targets = {r[0] for r in con.execute('SELECT DISTINCT target FROM kg_mentions UNION SELECT DISTINCT object_id FROM kg_triples')}
    known = {r[0] for r in con.execute('SELECT id FROM kg_entities')}
    person = dict(con.execute("SELECT person_id, canonical_id FROM kg_person_map WHERE person_id LIKE 'wiki:%'"))
    film = dict(con.execute("SELECT film_id, canonical_id FROM kg_film_map WHERE film_id LIKE 'wiki:%'"))
    pending = []
    for t in targets - known:
        title = t[5:].replace('_', ' ')
        if t in person:
            con.execute("INSERT INTO kg_entities VALUES(?,?,'Person',NULL,NULL,?)", (t, title, person[t]))
        elif t in film:
            con.execute("INSERT INTO kg_entities VALUES(?,?,'Film',NULL,NULL,?)", (t, title, film[t]))
        else:
            pending.append(t)
    con.commit()
    log(f'Typing {len(pending)} link targets via Wikidata ({len(targets) - len(pending)} resolved from the KG)')
    for start in range(0, len(pending), 50):
        batch = pending[start:start + 50]
        titles = '|'.join(t[5:] for t in batch)
        try:
            data = client.json('https://www.wikidata.org/w/api.php?action=wbgetentities&sites=enwiki&props=descriptions|claims|sitelinks'
                               '&sitefilter=enwiki&languages=en&format=json&titles=' + quote(titles, safe='|'))
        except Exception as exc:
            log(f'Wikidata batch failed: {exc}'); continue
        by_title = {}
        for qid, entity in data.get('entities', {}).items():
            if 'missing' in entity:
                continue
            p31 = {c['mainsnak'].get('datavalue', {}).get('value', {}).get('id') for c in entity.get('claims', {}).get('P31', [])}
            desc = entity.get('descriptions', {}).get('en', {}).get('value', '')
            for site in entity.get('sitelinks', {}).values():
                by_title[site['title']] = (qid, p31, desc)
        for t in batch:
            qid, p31, desc = by_title.get(t[5:].replace('_', ' '), (None, set(), ''))
            con.execute('INSERT OR REPLACE INTO kg_entities VALUES(?,?,?,?,?,NULL)',
                        (t, t[5:].replace('_', ' '), classify(p31, desc), qid, desc))
        con.commit()
        if start // 50 % 40 == 0:
            log(f'Typed {start + len(batch)}/{len(pending)}')
    return dict(con.execute('SELECT type, count(*) FROM kg_entities GROUP BY type').fetchall())


def classify(p31: set, description: str) -> str:
    if 'Q5' in p31:
        return 'Person'
    if re.search(r'\bindustry\b|\bcinema of\b|\bfilm genre\b|\bfilm movement\b', description, re.I):
        return 'Concept'  # "film industry of Thailand" is not a film
    if p31 & {'Q11424', 'Q24862', 'Q506240', 'Q229390', 'Q202866'}:
        return 'Film'
    for kind, pattern in TYPE_HINTS:
        if re.search(pattern, description, re.I):
            return kind
    return 'Concept'


def apply_rules(con: sqlite3.Connection) -> int:
    """Stage C: cue patterns within 80 chars before the anchor, constrained by object type."""
    con.execute("DELETE FROM kg_triples WHERE method IN ('link_rule','link_mention')")
    types = dict(con.execute('SELECT id, type FROM kg_entities'))
    rows = con.execute('SELECT film_id, section, sentence, anchor, target, offset, source_url FROM kg_mentions').fetchall()
    films_in = {}
    for film_id, _, sentence, _, target, offset, _ in rows:
        if types.get(target) == 'Film':
            films_in.setdefault((film_id, sentence), []).append((offset, target))
    out = []
    for film_id, section, sentence, anchor, target, offset, url in rows:
        kind = types.get(target, 'Concept')
        window_start = max(0, offset - 80)
        window = sentence[window_start:offset].lower()
        relation = None
        for pattern, rel, allowed in CUES:
            match = None
            if kind in allowed:
                match = list(re.finditer(pattern, window))
            if match:
                relation, cue_at = rel, window_start + match[-1].start()
                break
        if relation == 'DEBUT_OF':
            out.append((target, 'DEBUTED_IN', film_id, film_id, sentence, url, 'link_rule', 0.8))
        elif relation:
            subject = film_id
            if relation in FILM_TO_FILM:
                # "Chandramukhi, ..., which was a remake of Manichitrathazhu": the subject is the film linked
                # just before the cue, not necessarily the page's own film.
                before = [(o, t) for o, t in films_in.get((film_id, sentence), []) if o < cue_at and t != target]
                # Only when a relative clause ties that film to the cue ("Chandramukhi, ..., which was a remake of");
                # "It is a sequel to X and a remake of Y" keeps the page film as subject.
                if before and re.search(r'\b(?:which|that|who)\b', sentence[before[-1][0]:cue_at]):
                    subject = before[-1][1]
            out.append((subject, relation, target, film_id, sentence, url, 'link_rule', 0.8))
        elif kind != 'Person':  # people links are mostly credits already; keep the rest as weak edges
            out.append((film_id, 'MENTIONS', target, film_id, sentence, url, 'link_mention', 0.4))
    con.executemany('INSERT OR IGNORE INTO kg_triples VALUES(?,?,?,?,?,?,?,?)', out)
    con.commit()
    return len(out)


LLM_SCHEMA = {'type': 'object', 'properties': {'triples': {'type': 'array', 'items': {
    'type': 'object', 'properties': {'entity': {'type': 'string'}, 'relation': {'type': 'string', 'enum': RELATIONS}},
    'required': ['entity', 'relation']}}}, 'required': ['triples']}
LLM_PROMPT = """You label relations between a Malayalam film and entities linked in one sentence from its Wikipedia article.
Film: {title} ({year})
Sentence: {sentence}
Linked entities (name -> type): {entities}
For each linked entity, choose the single relation FILM -[relation]-> entity from: {relations}.
Use MENTIONS when the sentence does not state a specific relation. Only use entities from the list."""


def llm_label(con: sqlite3.Connection, limit_films: int = 200, log=print) -> dict:
    """Stage D: relabel weak MENTIONS edges with a small local model, one call per sentence."""
    import hashlib
    con.executescript(SCHEMA)
    films = [r[0] for r in con.execute("""SELECT t.film_id FROM kg_triples t JOIN kg_films f ON f.id=t.film_id
        WHERE t.method='link_mention' GROUP BY t.film_id
        ORDER BY (SELECT count(*) FROM kg_credits c WHERE c.film_id=t.film_id) DESC LIMIT ?""", (limit_films,))]
    stats = {'calls': 0, 'labelled': 0, 'still_mentions': 0, 'rejected': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    started = time.time()
    for n, film_id in enumerate(films, 1):
        title, year = con.execute('SELECT title, year FROM kg_films WHERE id=?', (film_id,)).fetchone()
        sentences = {}
        for sentence, target, url in con.execute("""SELECT evidence, object_id, source_url FROM kg_triples
                WHERE film_id=? AND method='link_mention'""", (film_id,)):
            sentences.setdefault((sentence, url), []).append(target)
        for (sentence, url), targets in sentences.items():
            digest = hashlib.sha1(sentence.encode()).hexdigest()
            if con.execute('SELECT 1 FROM kg_llm_done WHERE film_id=? AND sentence_hash=?', (film_id, digest)).fetchone():
                continue
            names = {}
            for t in targets:
                row = con.execute('SELECT title, type FROM kg_entities WHERE id=?', (t,)).fetchone()
                names[row[0] if row else t[5:].replace('_', ' ')] = (t, row[1] if row else 'Concept')
            prompt = LLM_PROMPT.format(title=title, year=year or '?', sentence=sentence,
                                       entities='; '.join(f'{k} -> {v[1]}' for k, v in names.items()),
                                       relations=', '.join(RELATIONS))
            try:
                out, usage = llm.chat([{'role': 'user', 'content': prompt}], schema=LLM_SCHEMA, max_tokens=300)
            except llm.LLMUnavailable as exc:
                log(f'LLM unavailable: {exc}'); return stats
            stats['calls'] += 1
            stats['prompt_tokens'] += usage.get('prompt_tokens') or 0
            stats['completion_tokens'] += usage.get('completion_tokens') or 0
            for item in out.get('triples', []):
                match = names.get(item.get('entity', ''))
                relation = item.get('relation')
                if not match or relation not in RELATIONS:
                    stats['rejected'] += 1  # hallucinated entity or relation: dropped, never stored
                    continue
                if relation == 'MENTIONS':
                    stats['still_mentions'] += 1
                    continue
                subject, obj, rel = (match[0], film_id, 'DEBUTED_IN') if relation == 'DEBUT_OF' else (film_id, match[0], relation)
                con.execute("INSERT OR IGNORE INTO kg_triples VALUES(?,?,?,?,?,?,'link_llm',0.65)",
                            (subject, rel, obj, film_id, sentence, url))
                stats['labelled'] += 1
            con.execute('INSERT OR IGNORE INTO kg_llm_done VALUES(?,?)', (film_id, digest))
            con.commit()
        if n % 10 == 0:
            log(f'LLM labelling {n}/{len(films)} films · {stats} · {time.time() - started:.0f}s')
    return stats
