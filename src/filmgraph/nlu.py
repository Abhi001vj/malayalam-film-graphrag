"""NLU: gazetteer NER + entity linking, then a 3-tier intent router.

Rules decide *which flow runs*; ML/LLM only help when rules are silent:
  tier 1  rules       high-precision regex over the delexicalised question (Dialogflow-style)
  tier 2  classifier  TF-IDF (word + char n-grams) + logistic regression trained on synthetic,
                      delexicalised utterances; probabilities re-weighted by slot compatibility
  tier 3  local LLM   constrained-JSON intent + mention extraction, only when tiers 1-2 are unsure;
                      its mentions are re-linked through the gazetteer, never trusted raw
Entities are always linked to KG ids before any query is built.
"""
from __future__ import annotations

import hashlib
import pickle
import re
import sqlite3
import time
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path

from . import llm
from .db import normalize

ROLE_WORDS = {
    'acted': 'actor', 'actor': 'actor', 'actors': 'actor', 'actress': 'actor', 'starred': 'actor', 'starring': 'actor',
    'star': 'actor', 'stars': 'actor', 'hero': 'actor', 'heroine': 'actor', 'lead': 'actor', 'cast': 'actor',
    'directed': 'director', 'direct': 'director', 'directs': 'director', 'directing': 'director', 'director': 'director', 'directors': 'director', 'direction': 'director',
    'produced': 'producer', 'producer': 'producer', 'wrote': 'writer', 'writer': 'writer', 'written': 'writer',
    'screenplay': 'writer', 'script': 'writer', 'scripted': 'writer', 'screenwriter': 'writer', 'story': 'writer',
    'music director': 'music_director', 'music': 'music_director', 'composer': 'music_director', 'composed': 'music_director',
    'scored': 'music_director', 'score': 'music_director', 'edited': 'editor', 'editor': 'editor',
    'singer': 'singer', 'sang': 'singer', 'sung': 'singer', 'sings': 'singer', 'playback': 'singer',
    'lyricist': 'lyricist', 'lyrics': 'lyricist', 'songwriter': 'lyricist', 'penned': 'lyricist',
    'cinematographer': 'cinematographer', 'cinematography': 'cinematographer', 'camera': 'cinematographer',
    'dop': 'cinematographer', 'shot by': 'cinematographer', 'choreographer': 'choreographer',
    'choreographed': 'choreographer', 'art director': 'production_designer', 'production designer': 'production_designer',
    'costume designer': 'costume_designer', 'assistant director': 'assistant_director',
}
INTENTS = {
    'collaboration': 'films two or more people worked on together (count or list)',
    'filmography': "one person's films, optionally by role/year (count or list)",
    'top_collaborators': 'who a person worked with most, optionally in a role',
    'film_info': 'overview / cast and crew of a named film',
    'film_crew': 'who held a specific role (director, music...) on a named film',
    'songs': 'songs by singer/composer/lyricist or songs in a film',
    'connection_path': 'how two people are connected through films (degrees of separation)',
    'by_attribute': 'films filtered by place, technology, genre, production company or year',
    'relation_query': 'remakes, sequels, adaptations/based-on, awards, debuts',
    'plot_search': 'find a film from a remembered story or theme',
    'similar_films': 'recommend films similar to a named film',
    'global_overview': 'big-picture questions about eras, camps, teams, trends (community summaries)',
    'help': 'greetings or what can you do',
    'handover': 'wants a human, reports wrong data or complains',
    'out_of_scope': 'not about Malayalam cinema',
}
# Slots an intent needs (used for slot-aware re-ranking and clarification prompts).
REQUIRES = {'collaboration': ('PERSON', 2), 'filmography': ('PERSON', 1), 'top_collaborators': ('PERSON', 1),
            'film_info': ('FILM', 1), 'film_crew': ('FILM', 1), 'connection_path': ('PERSON', 2),
            'similar_films': ('FILM', 1)}

RULES = [
    ('handover', r'\b(?:human|real person|agent|support team|customer care|wrong (?:answer|data|info)|incorrect|report (?:an? )?(?:error|issue|mistake)|complain)'),
    ('help', r'^(?:hi|hello|hey|namaskaram|help|what can you do|who are you|how does this work)\b[\s?!.]*$'),
    ('connection_path', r'\b(?:connected|connection between|degrees? of separation|separation between|bacon number|link between|path (?:from|between))\b'),
    ('relation_query', r'\b(?:remake[sd]?|remade|sequels?|prequels?|based on|adapt(?:ed|ation)s?|novels?|awards?|won|national award|debut(?:ed)?|inspired by)\b'),
    ('similar_films', r'\b(?:similar to|films? like|movies? like|something like|if i (?:liked|loved|enjoyed)|recommend)\b.*\bFILM\b'),
    ('top_collaborators', r'\b(?:most|frequent(?:ly)?|favou?rite|regular|usual|go-?to)\b.*\b(?:collaborat\w*|worked with|partners?|director|co-?stars?|composer|team)\b|\bworked with the most\b'),
    ('film_crew', r'^(?:who|which \w+)\s+(?:directed|composed|wrote|scored|shot|edited|produced|sang|choreographed|did (?:the )?(?:music|camera|editing|lyrics|script))\b.*\bFILM\b'),
    ('songs', r'\bsongs?\b'),
    ('global_overview', r'\b(?:overview|trends?|eras?|camps?|circles?|groups?|clusters?|communit(?:y|ies)|dominated|summari[sz]e|landscape|golden age|new generation|movement)\b'),
    ('out_of_scope', r'\b(?:weather|cricket|stock|recipe|python|javascript|book (?:a )?tickets?|showtimes?)\b'),
]

TEMPLATES = {
    'collaboration': ['how many films did PERSON and PERSON do together', 'how many movies have PERSON and PERSON acted together',
        'number of films with PERSON and PERSON', 'how many times did PERSON work with PERSON', 'films starring PERSON and PERSON',
        'list movies of PERSON and PERSON together', 'PERSON ROLE and PERSON ROLE films', 'movies directed by PERSON starring PERSON',
        'how many films did PERSON direct with PERSON in the lead', 'PERSON and PERSON movies in the DECADE',
        'show films where PERSON acted and PERSON wrote the script', 'did PERSON and PERSON ever work together',
        'count of PERSON PERSON movies', 'which films have both PERSON and PERSON', 'PERSON with PERSON',
        'PERSON PERSON PERSON films together', 'films of PERSON written by PERSON', 'PERSON ROLE PERSON ROLE how many',
        'movies with music by PERSON starring PERSON', 'together PERSON PERSON films after YEAR'],
    'filmography': ['films of PERSON', 'PERSON movies', 'list all PERSON films', 'how many films has PERSON acted in',
        'what films did PERSON direct', 'movies directed by PERSON', 'PERSON filmography', 'PERSON films in the DECADE',
        'how many movies did PERSON compose music for', 'films written by PERSON', 'show me PERSON films from YEAR',
        'which movies did PERSON produce', 'PERSON as ROLE', 'PERSON ROLE films', 'number of films PERSON shot as cinematographer',
        'what has PERSON done', 'PERSON recent films', 'PERSON movies between YEAR and YEAR'],
    'top_collaborators': ['who has PERSON worked with the most', 'frequent collaborators of PERSON', 'which director has PERSON worked with most',
        'PERSON favourite music director', 'who are PERSON regular co stars', 'most common writer for PERSON',
        'top collaborators of PERSON', 'which actor appeared most in PERSON films', 'PERSON go to cinematographer',
        'who does PERSON usually work with'],
    'film_info': ['tell me about FILM', 'FILM', 'details of FILM', 'cast of FILM', 'who acted in FILM', 'FILM movie',
        'info on FILM YEAR', 'what is FILM about', 'cast and crew of FILM', 'show FILM credits', 'FILM film details',
        'who are the actors in FILM'],
    'film_crew': ['who directed FILM', 'who composed the music for FILM', 'who wrote FILM', 'cinematographer of FILM',
        'music director of FILM', 'who edited FILM', 'director of FILM', 'who produced FILM', 'who did the music in FILM',
        'FILM director', 'FILM music by', 'who was the camera man for FILM'],
    'songs': ['songs sung by PERSON', 'songs of PERSON composed by PERSON', 'songs in FILM', 'PERSON PERSON songs',
        'which songs did PERSON write lyrics for', 'list songs from FILM', 'PERSON hit songs', 'duets of PERSON and PERSON',
        'songs with lyrics by PERSON and music by PERSON', 'how many songs did PERSON sing for PERSON'],
    'connection_path': ['how is PERSON connected to PERSON', 'degrees of separation between PERSON and PERSON',
        'link between PERSON and PERSON', 'connect PERSON to PERSON', 'shortest path from PERSON to PERSON',
        'PERSON number of PERSON', 'how are PERSON and PERSON related through films'],
    'by_attribute': ['films shot in PLACE', 'movies filmed at PLACE', 'films using TECH', 'TECH films', 'GENRE films of the DECADE',
        'first TECH malayalam film', 'films produced by ORG', 'movies released in YEAR', 'GENRE movies', 'films shot in PLACE in the DECADE',
        'list ORG productions', 'which films were shot in PLACE', 'malayalam films from YEAR', 'films made with TECH technology'],
    'relation_query': ['which films are remakes', 'remakes of FILM', 'films based on novels', 'which films are based on WORK',
        'films that won AWARD', 'sequel of FILM', 'debut film of PERSON', 'films adapted from novels by PERSON',
        'which malayalam films were remade in hindi', 'FILM remade in tamil', 'award winning films of the DECADE'],
    'plot_search': ['movie where a man pretends to be a ghost', 'film about a family hiding a murder', 'story of a fisherman and his daughter',
        'the one with a haunted house and a psychiatrist', 'film where a boy wants to become a police officer but becomes a rowdy',
        'movie about a man who loses his memory', 'film about friends on a trip that goes wrong', 'a film set during floods',
        'movie with a superhero who gets powers from lightning', 'story about a cable tv operator who covers up a crime',
        'film about a girl stuck in a cave', 'movie where the hero vows not to wear slippers until revenge',
        'a thriller about a serial killer in kochi', 'comedy about two unemployed friends who go to madras',
        'that movie about a bull escaping in a village', 'film about a woman trapped in kitchen work after marriage'],
    'similar_films': ['films like FILM', 'movies similar to FILM', 'recommend something like FILM', 'if i liked FILM what should i watch',
        'suggest films similar to FILM', 'what to watch after FILM'],
    'global_overview': ['what were the major filmmaker groups in the DECADE', 'give me an overview of malayalam cinema in the DECADE',
        'which teams dominated the DECADE', 'summarize the PERSON camp', 'what are the main collaboration circles',
        'describe the new generation movement', 'golden age of malayalam cinema', 'trends in malayalam films after YEAR',
        'who were the key people of the DECADE era'],
    'help': ['hi', 'hello', 'what can you do', 'help', 'how do i use this', 'hey there', 'what questions can i ask'],
    'handover': ['talk to a human', 'this answer is wrong', 'report an error', 'i want to speak to support', 'the data is incorrect',
        'connect me to an agent', 'this is not right'],
    'out_of_scope': ['what is the weather today', 'book tickets for tonight', 'write python code', 'who won the cricket match',
        'tell me a joke', 'what is the capital of france', 'best biryani in kochi', 'latest hollywood news'],
}


class Gazetteer:
    """Longest-match dictionary NER over KG names, aliases, titles, places, technologies."""

    def __init__(self, con: sqlite3.Connection):
        con.row_factory = sqlite3.Row
        self.entries: dict[str, list[dict]] = {}
        common = _common_words()
        for r in con.execute('SELECT a.normalized, a.person_id, p.name, p.film_count FROM kg_aliases a JOIN kg_people p ON p.id=a.person_id WHERE p.film_count>=1'):
            n = r['normalized']
            if len(n) < 3 or (' ' not in n and (len(n) < 4 or ((n in common or n.rstrip('s') in common) and r['film_count'] < 50))):
                continue
            self._add(n, {'type': 'PERSON', 'id': r['person_id'], 'name': r['name'], 'weight': r['film_count'],
                          'exact': normalize(r['name']) == n})
        for r in con.execute('SELECT id, title, year FROM kg_films'):
            n = normalize(r['title'])
            if len(n) >= 3:
                self._add(n, {'type': 'FILM', 'id': r['id'], 'name': r['title'], 'year': r['year'], 'weight': 1,
                              'needs_case': ' ' not in n and n in common})
        for r in con.execute("""SELECT f.value, m.canonical_id FROM facts f JOIN kg_film_map m ON m.film_id=f.film_id WHERE f.kind='title_alias'
                                UNION SELECT f.title, m.canonical_id FROM films f JOIN kg_film_map m ON m.film_id=f.id"""):
            n = normalize(r[0])
            if len(n) >= 4 and n not in common:
                t = con.execute('SELECT title, year FROM kg_films WHERE id=?', (r[1],)).fetchone()
                if not any(e['id'] == r[1] for e in self.entries.get(n, [])):
                    self._add(n, {'type': 'FILM', 'id': r[1], 'name': t[0], 'year': t[1], 'weight': 1})
        for kind, etype in (('location', 'PLACE'), ('technology', 'TECH'), ('genre', 'GENRE'), ('company', 'ORG')):
            for value, count in con.execute('SELECT value, count(*) FROM kg_facts WHERE kind=? GROUP BY value', (kind,)):
                n = normalize(value)
                if len(n) >= 3 and (etype != 'GENRE' or n not in {'film', 'films'}):
                    self._add(n, {'type': etype, 'id': value, 'name': value, 'weight': count})
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_entities'").fetchone():
            for r in con.execute("""SELECT e.id, e.title, e.type, count(*) n FROM kg_entities e JOIN kg_triples t ON t.object_id=e.id
                    WHERE e.type IN ('Place','Award','Work','Organization') AND e.kg_id IS NULL GROUP BY e.id HAVING n>=2"""):
                n = normalize(re.sub(r'\s*\(.*?\)', '', r['title']))
                if len(n) >= 4 and n not in common:
                    self._add(n, {'type': {'Place': 'PLACE', 'Award': 'AWARD', 'Work': 'WORK', 'Organization': 'ORG'}[r['type']],
                                  'id': r['id'], 'name': r['title'], 'weight': r['n']})
        self.max_len = max(len(k.split()) for k in self.entries)
        self.fuzzy_pool = {k: v for k, v in self.entries.items() if any(e['type'] == 'PERSON' and e['weight'] >= 5 for e in v)}

    def _add(self, key, entry):
        self.entries.setdefault(key, []).append(entry)

    def tag(self, question: str) -> list[dict]:
        clean = normalize(question)
        tokens = clean.split()
        spans, i = [], 0
        while i < len(tokens):
            # "mohan lal" -> "mohanlal": a concatenated bigram that is a well-known person wins.
            if i + 1 < len(tokens):
                joined = [e for e in self.entries.get(tokens[i] + tokens[i + 1], []) if e['type'] == 'PERSON' and e['weight'] >= 10]
                if joined:
                    spans.append({'text': tokens[i] + ' ' + tokens[i + 1], 'start': i, 'end': i + 2, 'candidates': joined,
                                  'corrected_to': tokens[i] + tokens[i + 1]})
                    i += 2
                    continue
            for n in range(min(self.max_len, len(tokens) - i), 0, -1):
                key = ' '.join(tokens[i:i + n])
                cands = [e for e in self.entries.get(key, []) if not (e.get('needs_case') and not _capitalised(question, key))]
                if cands and key not in ROLE_WORDS and key not in _STOP:
                    spans.append({'text': key, 'start': i, 'end': i + n, 'candidates': cands})
                    i += n
                    break
            else:
                i += 1
        # Spelling variants: "mamooty", "mohan lal" -> fuzzy match against frequent person aliases.
        taken = {t for s in spans for t in range(s['start'], s['end'])}
        for i, tok in enumerate(tokens):
            if i in taken or len(tok) < 5 or tok in _STOP or tok in ROLE_WORDS or tok.isdigit():
                continue
            joined = tok + (tokens[i + 1] if i + 1 < len(tokens) and i + 1 not in taken else '')
            for probe, width in ((joined, 2), (tok, 1)):
                match = get_close_matches(probe, self.fuzzy_pool, n=1, cutoff=0.86 if width == 1 else 0.9)
                if match:
                    spans.append({'text': tok if width == 1 else ' '.join(tokens[i:i + 2]), 'start': i, 'end': i + width,
                                  'candidates': self.fuzzy_pool[match[0]], 'corrected_to': match[0]})
                    taken.update(range(i, i + width))
                    break
        return sorted(spans, key=lambda s: s['start'])


@lru_cache(maxsize=1)
def _common_words() -> frozenset:
    path = Path('/usr/share/dict/words')
    if path.exists():
        words = {w.strip().lower() for w in path.read_text().split()}
    else:  # serverless images lack a system dictionary; ship the subset that collides with KG names
        import json
        words = set(json.loads((Path(__file__).parent / 'models' / 'common_words.json').read_text()))
    return frozenset(words | _STOP)


def _capitalised(question, key):
    return re.search(r'(?<!\w)' + re.escape(key) + r'(?!\w)', question, re.I) and \
        any(m[0][0].isupper() or m[0].startswith('"') for m in re.finditer(r'"?\b' + re.escape(key) + r'\b', question, re.I))


_STOP = set('a an and are acted about by can did do does film films find for from had has have how i in is it me movie movies of on or '
            'the there this was were what when where which who with year many times together all list show tell give both also malayalam '
            'cinema please than after before since to until during between their his her its one ones best top new old first last'.split())


def link(spans: list[dict], question: str) -> dict:
    """Pick one KG entity per span. Ambiguity (comparable candidates) is kept for clarification."""
    entities, ambiguous = [], []
    lowered = normalize(question)
    for s in spans:
        cands = s['candidates']
        types = {c['type'] for c in cands}
        before = ' '.join(lowered.split()[max(0, s['start'] - 2):s['start']])
        if 'PLACE' in types and re.search(r'\b(?:in|at|near|around)$', before):
            cands = [c for c in cands if c['type'] == 'PLACE']
            types = {'PLACE'}
        if 'WORK' in types and types & {'FILM', 'PERSON'} and not re.search(r'\b(?:novel|book|story|play)\b', lowered):
            cands = [c for c in cands if c['type'] != 'WORK']  # "Chemmeen" is the film unless the novel is asked for
            types = {c['type'] for c in cands}
        if {'PERSON', 'FILM'} <= types:
            prefer = 'FILM' if re.search(r'\b(?:film|movie|in|about|like|songs in|cast of|directed|wrote)$', before) else 'PERSON'
            cands = [c for c in cands if c['type'] == prefer]
        if not cands:
            continue
        exact = [c for c in cands if c.get('exact')]
        top = max((c['weight'] for c in cands if c['type'] == 'PERSON'), default=0)
        # Stray source aliases never beat a real name match, unless the alias owner is far better known
        # ("Chithra" -> K. S. Chithra with 494 films, not the actress Chithra with 68).
        if exact and max(c['weight'] for c in exact) * 5 >= top:
            cands = [c for c in cands if c.get('exact') or c['type'] != 'PERSON']
        ranked = sorted(cands, key=lambda c: -c['weight'])
        best = ranked[0]
        same = [c for c in ranked if c['type'] == best['type']]
        if best['type'] == 'PERSON' and len(same) > 1 and same[1]['weight'] * 3 > best['weight'] and same[1]['weight'] >= 3:
            ambiguous.append({'text': s['text'], 'options': [{'id': c['id'], 'name': c['name'], 'films': c['weight']} for c in same[:4]]})
        if best['type'] == 'FILM' and len(same) > 1:
            years = re.findall(r'\b(?:19|20)\d{2}\b', question)
            pick = next((c for c in same if str(c.get('year')) in years), None)
            if pick:
                best = pick
            elif len({c['id'] for c in same}) > 1:
                ambiguous.append({'text': s['text'], 'options': [{'id': c['id'], 'name': f"{c['name']} ({c.get('year') or '?'})"} for c in same[:4]]})
        entity = {'type': best['type'], 'id': best['id'], 'name': best['name'], 'text': s['text'], 'start': s['start'], 'end': s['end']}
        if best.get('year'):
            entity['year'] = best['year']
        if s.get('corrected_to'):
            entity['corrected_from'] = s['text']
        entities.append(entity)
    return {'entities': entities, 'ambiguous': ambiguous}


def assign_roles(question: str, entities: list[dict]) -> None:
    """Attach role words to the nearest PERSON mention ("Mohanlal actor and Priyadarshan director")."""
    tokens = normalize(question).split()
    people = [e for e in entities if e['type'] == 'PERSON']
    if not people:
        return
    found = []
    i = 0
    while i < len(tokens):
        two = ' '.join(tokens[i:i + 2])
        if two in ROLE_WORDS:
            found.append((i, i + 2, ROLE_WORDS[two])); i += 2
        elif tokens[i] in ROLE_WORDS and not any(e['start'] <= i < e['end'] for e in entities):
            found.append((i, i + 1, ROLE_WORDS[tokens[i]])); i += 1
        else:
            i += 1
    for start, end, role in found:
        def distance(p):
            gap = tokens[min(end, p['end']):max(start, p['start'])]
            penalty = 50 if {'and', 'with', 'or'} & set(gap) and not (role == 'actor' and start > p['end']) else 0
            # "directed by X" binds forward; "X directed" binds backward
            forward = p['start'] >= end
            # "composed by X" binds forward; "X composed" binds backward
            by_follows = end < len(tokens) and tokens[end] == 'by'
            bias = 0 if forward == by_follows or (forward and not by_follows and start == 0) else 3
            return (p['start'] - end if forward else start - p['end'] + 1) + penalty + bias
        target = min(people, key=distance)
        if distance(target) < 8:
            target.setdefault('roles', [])
            if role not in target['roles']:
                target['roles'].append(role)
    # "PERSON and PERSON acted together" -> actor for every person without an explicit role
    if found and all(r == 'actor' for *_, r in found):
        for p in people:
            p['roles'] = ['actor']


def time_slots(question: str) -> dict:
    q = question.lower()
    slots = {}
    decade = re.search(r"\b(?:(19|20)(\d)0'?s|'?(\d)0'?s)\b", q)
    if decade:
        start = int((decade[1] or ('20' if decade[3] in '012' else '19')) + (decade[2] or decade[3]) + '0')
        slots.update(year_from=start, year_to=start + 9)
    years = [int(y) for y in re.findall(r'\b(?:19|20)\d{2}\b', q)]
    if len(years) >= 2:
        slots.update(year_from=min(years), year_to=max(years))
    elif years:
        y = years[0]
        if re.search(rf'\b(?:after|since|from|post)\s+{y}', q):
            slots['year_from'] = y + (1 if 'after' in q else 0)
        elif re.search(rf'\b(?:before|until|till|pre)\s+{y}', q):
            slots['year_to'] = y - (1 if 'before' in q else 0)
        elif not decade:
            slots.update(year_from=y, year_to=y)
    if re.search(r'\bhow many\b|\bnumber of\b|\bcount\b|\bhow often\b', q):
        slots['aggregate'] = 'count'
    return slots


def delexicalise(question: str, entities: list[dict]) -> str:
    tokens = normalize(question).split()
    for e in sorted(entities, key=lambda e: -e['start']):
        tokens[e['start']:e['end']] = [e['type']]
    text = ' '.join(tokens)
    text = re.sub(r"\b(?:19|20)\d0s\b|\b\d0s\b", 'DECADE', text)
    return re.sub(r'\b(?:19|20)\d{2}\b', 'YEAR', text)


class PortableClassifier:
    """sklearn-free inference for the exported TF-IDF + logistic regression (serverless bundles need only NumPy)."""

    def __init__(self, data: dict):
        import numpy as np
        self.np, self.d = np, data
        self.coef, self.intercept = np.asarray(data['coef']), np.asarray(data['intercept'])
        self.classes_ = data['classes']

    def _vector(self, text, part):
        np = self.np
        vocab, idf = part['vocab'], part['idf']
        if part['analyzer'] == 'word':
            tokens = re.findall(part['token_pattern'], text.lower())
            grams = tokens + [' '.join(tokens[i:i + 2]) for i in range(len(tokens) - 1)]
        else:  # sklearn char_wb
            grams = []
            for w in re.sub(r'\s\s+', ' ', text.lower()).split():
                w = ' ' + w + ' '
                for n in range(3, 6):
                    offset = 0
                    grams.append(w[offset:offset + n])
                    while offset + n < len(w):
                        offset += 1
                        grams.append(w[offset:offset + n])
                    if offset == 0:
                        break
        vec = np.zeros(len(idf))
        for g in grams:
            if g in vocab:
                vec[vocab[g]] += 1
        nz = vec > 0
        vec[nz] = (1 + np.log(vec[nz])) * np.asarray(idf)[nz]
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def predict_proba(self, texts):
        np = self.np
        out = []
        for t in texts:
            x = np.concatenate([self._vector(t, part) for part in self.d['parts']])
            z = self.coef @ x + self.intercept
            e = np.exp(z - z.max())
            out.append(e / e.sum())
        return np.asarray(out)


def export_portable(model, path: Path) -> None:
    import json
    union, lr = model.steps[0][1], model.steps[1][1]
    parts = []
    for _, vec in union.transformer_list:
        parts.append({'analyzer': 'word' if vec.analyzer == 'word' else 'char_wb', 'token_pattern': vec.token_pattern,
                      'vocab': {k: int(v) for k, v in vec.vocabulary_.items()}, 'idf': [round(float(x), 6) for x in vec.idf_]})
    path.write_text(json.dumps({'classes': list(lr.classes_), 'coef': lr.coef_.round(6).tolist(),
                                'intercept': lr.intercept_.round(6).tolist(), 'parts': parts}))


class IntentClassifier:
    """TF-IDF + logistic regression over delexicalised synthetic utterances."""
    PATH = Path('data/models/intent.pkl')
    PORTABLE = Path(__file__).parent / 'models' / 'intent.json'

    def __init__(self):
        digest = hashlib.sha1(repr(sorted(TEMPLATES.items())).encode()).hexdigest()
        try:
            import sklearn  # noqa: F401
        except ImportError:  # serverless: use the exported weights shipped with the package
            import json
            self.model = PortableClassifier(json.loads(self.PORTABLE.read_text()))
            return
        if self.PATH.exists():
            saved = pickle.loads(self.PATH.read_bytes())
            if saved.get('digest') == digest:
                self.model = saved['model']
                return
        self.model = self.train()
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        self.PATH.write_bytes(pickle.dumps({'digest': digest, 'model': self.model}))
        self.PORTABLE.parent.mkdir(parents=True, exist_ok=True)
        export_portable(self.model, self.PORTABLE)

    @staticmethod
    def dataset():
        X, y = [], []
        fillers = {'PERSON': ['PERSON'], 'ROLE': ['actor', 'director', 'writer', 'music director', 'singer'],
                   'DECADE': ['DECADE'], 'YEAR': ['YEAR'], 'FILM': ['FILM'], 'PLACE': ['PLACE'], 'TECH': ['TECH'],
                   'GENRE': ['GENRE'], 'ORG': ['ORG'], 'WORK': ['WORK'], 'AWARD': ['AWARD']}
        for intent, phrases in TEMPLATES.items():
            for p in phrases:
                X.append(p); y.append(intent)
                for role in fillers['ROLE'][:2]:
                    if 'ROLE' in p:
                        X.append(p.replace('ROLE', role)); y.append(intent)
                X.append('please ' + p); y.append(intent)
                X.append(p + ' in malayalam'); y.append(intent)
        return X, y

    @staticmethod
    def train():
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline, make_union
        from sklearn.feature_extraction.text import TfidfVectorizer
        X, y = IntentClassifier.dataset()
        features = make_union(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, token_pattern=r'[A-Za-z_]+'),
                              TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), sublinear_tf=True))
        model = make_pipeline(features, LogisticRegression(C=8, max_iter=3000, class_weight='balanced'))
        return model.fit(X, y)

    def predict(self, text: str) -> dict[str, float]:
        probs = self.model.predict_proba([text])[0]
        return dict(zip(self.model.classes_, map(float, probs)))


def slot_compatible(intent: str, entities: list[dict]) -> float:
    need = REQUIRES.get(intent)
    if not need:
        return 1.0
    have = sum(e['type'] == need[0] for e in entities)
    return 1.0 if have >= need[1] else 0.35 if have else 0.15


LLM_SCHEMA = {'type': 'object', 'properties': {
    'intent': {'type': 'string', 'enum': list(INTENTS)},
    'people': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'role': {'type': 'string'}}, 'required': ['name']}},
    'films': {'type': 'array', 'items': {'type': 'string'}},
    'confidence': {'type': 'number'}}, 'required': ['intent', 'people', 'films', 'confidence']}


@lru_cache(maxsize=1)
def _classifier():
    return IntentClassifier()


_GAZ: dict = {}


def gazetteer(con, key: str = 'default') -> Gazetteer:
    if key not in _GAZ:
        _GAZ[key] = Gazetteer(con)
    return _GAZ[key]


def understand(con: sqlite3.Connection, question: str, use_llm: bool = True, threshold: float = 0.5) -> dict:
    """Question -> {intent, confidence, method, entities, slots, ambiguous, trace}."""
    t0 = time.perf_counter()
    gaz = gazetteer(con)
    linked = link(gaz.tag(question), question)
    entities = linked['entities']
    assign_roles(question, entities)
    slots = time_slots(question)
    ner_ms = round((time.perf_counter() - t0) * 1000, 1)
    delex = delexicalise(question, entities)
    trace = {'ner_ms': ner_ms, 'delexicalised': delex}

    intent, method, confidence = None, None, 0.0
    for name, pattern in RULES:
        if re.search(pattern, delex if 'FILM' in pattern else question.lower()) or re.search(pattern, delex):
            if slot_compatible(name, entities) >= 1.0 or name in ('songs', 'relation_query', 'global_overview', 'handover', 'help', 'out_of_scope'):
                intent, method, confidence = name, 'rule', 0.95
                trace['rule'] = pattern
                break
    t1 = time.perf_counter()
    probs = _classifier().predict(delex)
    reweighted = {k: v * slot_compatible(k, entities) for k, v in probs.items()}
    total = sum(reweighted.values()) or 1
    reweighted = {k: v / total for k, v in reweighted.items()}
    ml_intent = max(reweighted, key=reweighted.get)
    trace['classifier_ms'] = round((time.perf_counter() - t1) * 1000, 1)
    trace['classifier_top3'] = sorted(((k, round(v, 3)) for k, v in reweighted.items()), key=lambda x: -x[1])[:3]
    if intent is None:
        intent, method, confidence = ml_intent, 'classifier', reweighted[ml_intent]
        # Entity shape is decisive when the classifier is torn: 2 people -> collaboration, a film -> film_info.
        people = sum(e['type'] == 'PERSON' for e in entities)
        films = sum(e['type'] == 'FILM' for e in entities)
        if confidence < threshold and not use_llm:
            if people >= 2: intent, method = 'collaboration', 'slot_heuristic'
            elif people == 1: intent, method = 'filmography', 'slot_heuristic'
            elif films: intent, method = 'film_info', 'slot_heuristic'
    if confidence < threshold and use_llm and llm.available():
        prompt = (f'Classify a question for a Malayalam cinema knowledge-graph assistant.\nIntents: '
                  + '; '.join(f'{k}: {v}' for k, v in INTENTS.items())
                  + f'\nExtract people (with role if stated: actor, director, writer, music_director, singer, lyricist, '
                    f'cinematographer, editor, producer) and film titles exactly as written.\nQuestion: {question}')
        try:
            out, usage = llm.chat([{'role': 'user', 'content': prompt}], schema=LLM_SCHEMA, max_tokens=200)
            trace['llm'] = {'output': out, **usage}
            if out.get('intent') in INTENTS:
                intent, method, confidence = out['intent'], 'llm', min(float(out.get('confidence') or .7), .9)
                # Re-link LLM mentions through the gazetteer: the model proposes, the KG disposes.
                for mention in out.get('people', []) + [{'name': f} for f in out.get('films', [])]:
                    extra = link(gaz.tag(mention['name']), mention['name'])['entities']
                    for e in extra:
                        if not any(x['id'] == e['id'] for x in entities):
                            e['start'] = e['end'] = -1
                            if mention.get('role') in set(ROLE_WORDS.values()):
                                e['roles'] = [mention['role']]
                            entities.append(e)
        except llm.LLMUnavailable as exc:
            trace['llm_error'] = str(exc)
    trace['total_ms'] = round((time.perf_counter() - t0) * 1000, 1)
    return {'intent': intent, 'confidence': round(confidence, 3), 'method': method, 'entities': entities,
            'slots': slots, 'ambiguous': linked['ambiguous'], 'trace': trace}
