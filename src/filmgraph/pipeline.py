"""GraphRAG orchestrator: NLU -> dialogue state -> intent flow -> retrieval -> grounded answer.

Same shape as a Grakn/Dialogflow assistant: the classified intent triggers a flow; each flow
fills a parameterised graph query from linked entities; the LLM is used inside flows only
for narration over retrieved context (never for numbers), and for NLU fallback.
Responses carry cards, a graph for the UI, a step-by-step trace, and dialogue context the
client sends back next turn (stateless server => works on serverless).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from . import llm, nlu, retrieval
from .graphstore import SQLiteStore, open_store

FOLLOW_UP = re.compile(r"^(?:and|also|what about|how about|only|just|with|in the|but|same|now|then)\b|\b(?:he|she|they|them|his|her|their|those|these|together)\b", re.I)
ROLE_LABEL = lambda r: r.replace('_', ' ')
SUGGESTIONS = ['How many films did Mohanlal and Mammootty do together?', 'Mohanlal actor and Priyadarshan director',
               'Who has Sreenivasan worked with the most?', 'How is Fahadh Faasil connected to Prem Nazir?',
               'Songs sung by K. J. Yesudas composed by Johnson', 'Movie where a family hides a murder from the police',
               'Which films are remakes of Manichitrathazhu?', 'Films like Kireedam', 'Who directed Chemmeen?',
               'What were the major filmmaker circles in the 80s?']


class Trace:
    def __init__(self):
        self.steps, self.t0 = [], time.perf_counter()

    def add(self, stage, started, **detail):
        self.steps.append({'stage': stage, 'ms': round((time.perf_counter() - started) * 1000, 1), **detail})


def answer(con: sqlite3.Connection, question: str, context: dict | None = None, overrides: dict | None = None,
           use_llm: bool = True, backend: str | None = None) -> dict:
    context = context or {}
    trace = Trace()
    t = time.perf_counter()
    nlu_out = nlu.understand(con, question, use_llm=use_llm)
    trace.add('nlu', t, intent=nlu_out['intent'], method=nlu_out['method'], confidence=nlu_out['confidence'],
              entities=[{k: e.get(k) for k in ('type', 'name', 'id', 'roles', 'corrected_from')} for e in nlu_out['entities']],
              slots=nlu_out['slots'], **nlu_out['trace'])
    entities, slots, intent = nlu_out['entities'], nlu_out['slots'], nlu_out['intent']
    for text, chosen in (overrides or {}).items():  # user picked an option in a clarification
        for e in entities:
            if e['text'] == text and isinstance(chosen, str):
                row = con.execute('SELECT name FROM kg_people WHERE id=?', (chosen,)).fetchone() or \
                      con.execute('SELECT title FROM kg_films WHERE id=?', (chosen,)).fetchone()
                if row:
                    e['id'], e['name'] = chosen, row[0]
        nlu_out['ambiguous'] = [a for a in nlu_out['ambiguous'] if a['text'] != text]

    # Dialogue state: carry entities/slots over for follow-ups ("what about with Priyadarshan?").
    t = time.perf_counter()
    carried = []
    prev = context.get('entities', [])
    if prev and context.get('intent') and FOLLOW_UP.search(question.strip()):
        people_now = [e for e in entities if e['type'] == 'PERSON']
        if context['intent'] == 'collaboration' and people_now and len(prev) >= 2:
            carried = prev[:-len(people_now)] if len(people_now) < len(prev) else []
        elif not any(e['type'] in ('PERSON', 'FILM') for e in entities):
            carried = prev
        entities = carried + entities
        if nlu_out['confidence'] < 0.6 or not any(e['type'] in ('PERSON', 'FILM') for e in nlu_out['entities']):
            intent = context['intent'] if not nlu.slot_compatible(intent, nlu_out['entities']) >= 1 or nlu_out['confidence'] < 0.6 else intent
        slots = {**{k: v for k, v in context.get('slots', {}).items() if k != 'aggregate'}, **slots}
    if carried or intent != nlu_out['intent']:
        trace.add('dialogue_state', t, carried=[e['name'] for e in carried], intent=intent)
    if intent in ('filmography',) and sum(e['type'] == 'PERSON' for e in entities) >= 2:
        intent = 'collaboration'

    store = open_store(con, backend) if backend != 'sqlite' else SQLiteStore(con)
    response = {'question': question, 'intent': intent, 'confidence': nlu_out['confidence'], 'method': nlu_out['method'],
                'entities': entities, 'slots': slots, 'backend': store.name, 'cards': [], 'graph': None,
                'suggestions': [], 'clarify': None}

    needed = nlu.REQUIRES.get(intent)
    ambiguous = [a for a in nlu_out['ambiguous'] if any(e['text'] == a['text'] for e in entities)]
    low = nlu_out['confidence'] < 0.35 and nlu_out['method'] != 'rule' and not carried
    if ambiguous and intent not in ('help', 'handover', 'out_of_scope'):
        a = ambiguous[0]
        response.update(answer=f"“{a['text']}” matches more than one entry. Which one did you mean?",
                        clarify={'text': a['text'], 'options': a['options']}, flow='clarify_entity')
    elif needed and sum(e['type'] == needed[0] for e in entities) < needed[1]:
        what = {'PERSON': 'person' if needed[1] == 1 else 'two people', 'FILM': 'film'}[needed[0]]
        response.update(answer=f"I can answer that as **{intent.replace('_', ' ')}**, but I need the {what}'s name. "
                               f"I couldn't find it in the catalogue; could you rephrase or check the spelling?",
                        flow='clarify_slot')
    elif low and intent not in ('plot_search',):
        strikes = context.get('low_confidence', 0) + 1
        if strikes >= 2:
            response.update(handover(con, question, 'repeated low confidence', context))
        else:
            response.update(answer="I'm not sure what you're asking. Try naming people, a film, a place or a remembered plot.",
                            suggestions=SUGGESTIONS[:4], flow='fallback')
        response['low_confidence'] = strikes
    else:
        flow = FLOWS.get(intent, flow_help)
        t = time.perf_counter()
        response.update(flow(con, store, question, entities, slots, use_llm, trace))
        response['flow'] = intent
    response['context'] = {'intent': response['intent'] if response.get('flow') not in ('clarify_entity', 'clarify_slot', 'fallback') else context.get('intent'),
                           'entities': [{k: e[k] for k in ('type', 'id', 'name', 'text') if k in e} | ({'roles': e['roles']} if e.get('roles') else {})
                                        for e in entities if e['type'] in ('PERSON', 'FILM')] or context.get('entities', []),
                           'slots': slots, 'low_confidence': response.get('low_confidence', 0)}
    response['trace'] = trace.steps
    response['total_ms'] = round((time.perf_counter() - trace.t0) * 1000, 1)
    _log(response)
    return response


# ---------------------------------------------------------------- flows

def _people(entities):
    return [{'id': e['id'], 'name': e['name'], 'roles': e.get('roles') or []} for e in entities if e['type'] == 'PERSON']


def _span(rows, slots=None):
    if slots and (slots.get('year_from') or slots.get('year_to')):
        return ''
    years = [r['year'] for r in rows if r.get('year')]
    return f' ({min(years)}–{max(years)})' if years else ''


def _who(p):
    return p['name'] + (' (' + '/'.join(map(ROLE_LABEL, p['roles'])) + ')' if p['roles'] else '')


def _years(slots):
    if slots.get('year_from') and slots.get('year_to'):
        return f" between {slots['year_from']} and {slots['year_to']}" if slots['year_from'] != slots['year_to'] else f" in {slots['year_from']}"
    return f" from {slots['year_from']}" if slots.get('year_from') else f" up to {slots['year_to']}" if slots.get('year_to') else ''


def flow_collaboration(con, store, q, entities, slots, use_llm, trace):
    people = _people(entities)
    t = time.perf_counter()
    rows, query = store.films_with(people, slots.get('year_from'), slots.get('year_to'))
    trace.add('graph_query', t, backend=store.name, template='films_with', query=query, params=[p['id'] for p in people], rows=len(rows))
    names = ' and '.join(_who(p) for p in people)
    if not rows:
        text = f'I found **no films** in the catalogue crediting {names}{_years(slots)}.'
    else:
        text = (f'**{len(rows)} film{"s" if len(rows) != 1 else ""}** credit {names}{_years(slots)}{_span(rows, slots)}. '
                f'Most recent: {", ".join(r["title"] + " (" + str(r["year"]) + ")" for r in rows[:3])}.')
    text += ' _Counted as distinct films in the imported catalogue; missing credits can lower it._'
    return _films_response(con, text, rows, [p['id'] for p in people], trace)


def flow_filmography(con, store, q, entities, slots, use_llm, trace):
    people = _people(entities)[:1]
    t = time.perf_counter()
    rows, query = store.films_with(people, slots.get('year_from'), slots.get('year_to'))
    trace.add('graph_query', t, backend=store.name, template='films_with', query=query, params=[people[0]['id']], rows=len(rows))
    roles = Counter(r[0] for r in con.execute('SELECT role FROM kg_credits WHERE person_id=?', (people[0]['id'],)))
    role_text = ', '.join(f'{ROLE_LABEL(r)} ×{n}' for r, n in roles.most_common(4))
    text = (f"**{_who(people[0])}** has **{len(rows)} film{'s' if len(rows) != 1 else ''}**{_years(slots)}{_span(rows, slots)} in the catalogue"
            + (f' (credits: {role_text})' if role_text and not people[0]['roles'] else '') + '.')
    if rows:
        text += f' Latest: {", ".join(r["title"] + " (" + str(r["year"]) + ")" for r in rows[:3])}.'
    return _films_response(con, text, rows, [people[0]['id']], trace)


def flow_top_collaborators(con, store, q, entities, slots, use_llm, trace):
    person = _people(entities)[0]
    other_roles = person['roles']  # "which director has X worked with" -> role describes the collaborator
    t = time.perf_counter()
    rows, query = store.top_collaborators(person['id'], other_roles, 12)
    trace.add('graph_query', t, backend=store.name, template='top_collaborators', query=query, params=[person['id'], other_roles], rows=len(rows))
    lines = [f"{i + 1}. **{r['name']}** — {r['films']} films ({r['roles'].replace('_', ' ')})" for i, r in enumerate(rows[:8])]
    text = f"{person['name']}'s most frequent {('/'.join(map(ROLE_LABEL, other_roles)) + ' ') if other_roles else ''}collaborators:\n" + '\n'.join(lines)
    graph = {'nodes': [_person_node(con, person['id'], focus=True)], 'edges': []}
    for r in rows:
        graph['nodes'].append({'id': r['id'], 'label': r['name'], 'group': r['roles'].split(',')[0], 'roles': r['roles'].split(','),
                               'size': r['films']})
        graph['edges'].append({'from': person['id'], 'to': r['id'], 'label': f"{r['films']} films", 'role': 'collaborated', 'value': r['films']})
    graph['highlight'] = [person['id']]
    return {'answer': text, 'graph': graph, 'table': rows,
            'suggestions': [f"How many films did {person['name']} and {rows[0]['name']} do together?"] if rows else []}


def flow_film_info(con, store, q, entities, slots, use_llm, trace):
    film = next(e for e in entities if e['type'] == 'FILM')
    t = time.perf_counter()
    credits, query = store.film_credits(film['id'])
    trace.add('graph_query', t, backend=store.name, template='film_credits', query=query, params=[film['id']], rows=len(credits))
    card = film_card(con, film['id'])
    by_role = {}
    for c in credits:
        by_role.setdefault(c['role'], []).append(c['name'])
    parts = [f"**{card['title']}** ({card['year'] or 'year unknown'})"]
    for role, label in (('director', 'directed by'), ('writer', 'written by'), ('music_director', 'music by'), ('cinematographer', 'shot by')):
        if by_role.get(role):
            parts.append(f'{label} {", ".join(by_role[role][:3])}')
    text = ', '.join(parts) + '.'
    if by_role.get('actor'):
        text += f' Starring {", ".join(by_role["actor"][:6])}.'
    relations, rq = store.related(film_id=film['id'], limit=12)
    trace.add('graph_query', time.perf_counter(), template='related', query=rq, rows=len(relations))
    if relations:
        text += ' ' + ' '.join(f"{ROLE_LABEL(r['relation'].lower()).capitalize()}: {r['object']}." for r in relations[:4] if r['relation'] != 'MENTIONS')
    if card.get('plot'):
        text += f"\n\n> {card['plot'][:420]}{'…' if len(card['plot']) > 420 else ''}"
    return {'answer': text, 'cards': [card], 'graph': film_graph(con, [film['id']], []), 'triples': relations,
            'suggestions': [f"Films like {card['title']}", f"Songs in {card['title']}"]}


def flow_film_crew(con, store, q, entities, slots, use_llm, trace):
    film = next(e for e in entities if e['type'] == 'FILM')
    words = nlu.normalize(q).split()
    roles = [nlu.ROLE_WORDS[w] for w in words if w in nlu.ROLE_WORDS] or \
            [nlu.ROLE_WORDS[a + ' ' + b] for a, b in zip(words, words[1:]) if a + ' ' + b in nlu.ROLE_WORDS] or ['director']
    roles = list(dict.fromkeys(roles))
    t = time.perf_counter()
    rows, query = store.film_credits(film['id'], roles)
    trace.add('graph_query', t, backend=store.name, template='film_credits', query=query, params=[film['id'], roles], rows=len(rows))
    names = list(dict.fromkeys(r['name'] for r in rows))
    title = f"{film['name']} ({film.get('year') or '?'})"
    text = (f"The {'/'.join(map(ROLE_LABEL, roles))} of **{title}**: **{', '.join(names)}**." if names
            else f"The catalogue has no {'/'.join(map(ROLE_LABEL, roles))} credit for {title}.")
    return {'answer': text, 'cards': [film_card(con, film['id'])], 'graph': film_graph(con, [film['id']], [r['id'] for r in rows]),
            'suggestions': [f"Tell me about {film['name']}"] + ([f'Films of {names[0]}'] if names else [])}


def flow_songs(con, store, q, entities, slots, use_llm, trace):
    people = _people(entities)
    for p in people:
        p['roles'] = [r for r in p['roles'] if r in ('singer', 'lyricist', 'music_director')]
    film = next((e for e in entities if e['type'] == 'FILM'), None)
    t = time.perf_counter()
    rows, query = store.songs(people, film['id'] if film else None, 200)
    trace.add('graph_query', t, backend=store.name, template='songs', query=query, params=[p['id'] for p in people], rows=len(rows))
    scope = ' and '.join(_who(p) for p in people) + (f" in {film['name']}" if film else '')
    text = f"**{len(rows)} song{'s' if len(rows) != 1 else ''}** credit {scope}{_span(rows)}."
    if rows:
        text += '\n' + '\n'.join(f"- {r['title']} — *{r['film']}* ({r['year'] or '?'})" for r in rows[:10])
    films = list(dict.fromkeys(r['film_id'] for r in rows))
    return {'answer': text, 'cards': [film_card(con, f) for f in films[:6]], 'graph': film_graph(con, films[:40], [p['id'] for p in people]),
            'table': rows[:100]}


def flow_connection_path(con, store, q, entities, slots, use_llm, trace):
    a, b = _people(entities)[:2]
    t = time.perf_counter()
    path, query = store.shortest_path(a['id'], b['id'])
    trace.add('graph_query', t, backend=store.name, template='shortest_path', query=query, params=[a['id'], b['id']], hops=max(0, len(path) - 1))
    if not path:
        return {'answer': f"No connection between {a['name']} and {b['name']} within 6 films in the catalogue."}
    steps, nodes, edges = [], [], []
    for i, node in enumerate(path):
        film = con.execute('SELECT title, year, wikipedia_url FROM kg_films WHERE id=?', (node,)).fetchone()
        if film:
            nodes.append({'id': node, 'label': film[0], 'group': 'film', 'year': film[1], 'url': film[2]})
        else:
            nodes.append(_person_node(con, node, focus=node in (a['id'], b['id'])))
        if i:
            prev = path[i - 1]
            person, fid = (node, prev) if film is None else (prev, node)
            role = con.execute('SELECT role FROM kg_credits WHERE person_id=? AND film_id=? LIMIT 1', (person, fid)).fetchone()
            edges.append({'from': person, 'to': fid, 'label': ROLE_LABEL(role[0]) if role else '', 'role': role[0] if role else ''})
    for i in range(0, len(path) - 2, 2):
        p1, f, p2 = (next(n['label'] for n in nodes if n['id'] == x) for x in path[i:i + 3])
        year = next(n.get('year') for n in nodes if n['id'] == path[i + 1])
        steps.append(f'{p1} → *{f}* ({year}) → {p2}')
    degrees = (len(path) - 1) // 2
    text = f"**{a['name']}** and **{b['name']}** are **{degrees} film{'s' if degrees != 1 else ''} apart**:\n" + '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(steps))
    films = path[1::2]
    return {'answer': text, 'graph': {'nodes': nodes, 'edges': edges, 'highlight': path}, 'cards': [film_card(con, f) for f in films], 'path': path}


RELATION_CUES = [(r'remade|remakes? (?:in|into)', ['REMADE_AS']), (r'remake', ['REMAKE_OF', 'REMADE_AS']), (r'sequel|prequel', ['SEQUEL_TO', 'HAS_SEQUEL']),
                 (r'based on|adapt|novel|book|story by', ['BASED_ON']), (r'award|won|winning', ['WON_AWARD', 'NOMINATED_FOR']),
                 (r'debut', ['DEBUTED_IN']), (r'inspired', ['INSPIRED_BY']), (r'festival|premiere', ['SCREENED_AT'])]


def flow_relation_query(con, store, q, entities, slots, use_llm, trace):
    relations = next((rels for pattern, rels in RELATION_CUES if re.search(pattern, q.lower())), [None])
    anchors = [e for e in entities if e['type'] in ('FILM', 'PERSON', 'WORK', 'AWARD', 'ORG', 'PLACE')]
    rows, queries = [], []
    t = time.perf_counter()
    for relation in relations:
        if anchors:
            for e in anchors:
                ids = [e['id']] + [r[0] for r in con.execute('SELECT id FROM kg_entities WHERE kg_id=?', (e['id'],))]
                for i in ids:
                    if e['type'] == 'FILM':
                        got, query = store.related(relation, film_id=i, limit=50)
                        rows += got; queries.append(query)
                    got, query = store.related(relation, object_id=i, limit=50)
                    rows += got; queries.append(query)
        else:
            got, query = store.related(relation, limit=60)
            rows += got; queries.append(query)
    if slots.get('year_from'):
        rows = [r for r in rows if r.get('year') and slots['year_from'] <= r['year'] <= slots.get('year_to', 3000)]
    seen, unique = set(), []
    for r in rows:
        key = (r['subject_id'], r['relation'], r['object_id'])
        if key not in seen:
            seen.add(key); unique.append(r)
    trace.add('graph_query', t, backend=store.name, template='related', query=queries[0] if queries else '', relations=relations, rows=len(unique))
    if not unique:
        return {'answer': 'No matching relations were extracted for that question. Relation triples come from linked phrases in Wikipedia prose, so coverage is partial.'}
    lines = []
    for r in unique[:12]:
        verb = ROLE_LABEL(r['relation'].lower())
        own = r['subject_id'] == r['film_id']
        subject = f"{r['film']} ({r['year'] or '?'})" if own else r['subject_id'].replace('wiki:', '').replace('_', ' ')
        lines.append(f"- *{subject}* **{verb}** {r['object']}  \n  <sub>“{r['evidence'][:160]}…” · {r['method']}"
                     + ('' if own else f" · from the {r['film']} article") + '</sub>')
    text = f"Found **{len(unique)}** relation{'s' if len(unique) != 1 else ''} ({', '.join(x or 'any' for x in relations)}):\n" + '\n'.join(lines)
    films = list(dict.fromkeys(r['film_id'] for r in unique))
    return {'answer': text, 'triples': unique[:50], 'cards': [film_card(con, f) for f in films[:6]], 'graph': triple_graph(con, unique[:60])}


def flow_by_attribute(con, store, q, entities, slots, use_llm, trace):
    facts, triple_ids = {}, set()
    for e in entities:
        kind = {'PLACE': 'location', 'TECH': 'technology', 'GENRE': 'genre', 'ORG': 'company'}.get(e['type'])
        if not kind:
            continue
        if e['id'].startswith('wiki:'):  # place/org entity from link triples
            rel = 'PRODUCED_BY' if e['type'] == 'ORG' else None
            got, _ = store.related(rel, object_id=e['id'], limit=300)
            triple_ids |= {r['film_id'] for r in got if r['relation'] in ('SHOT_AT', 'SET_IN', 'PRODUCED_BY', 'DISTRIBUTED_BY', 'MENTIONS')}
        else:
            facts[kind] = e['id']
    t = time.perf_counter()
    rows, query = store.films_with(_people(entities), slots.get('year_from'), slots.get('year_to'), facts or None) if (facts or not triple_ids) else ([], '')
    if triple_ids:
        extra = con.execute(f"SELECT id, title, year FROM kg_films WHERE id IN ({','.join('?' * len(triple_ids))}) ORDER BY year DESC", list(triple_ids)).fetchall()
        rows += [dict(zip(('id', 'title', 'year'), r)) for r in extra
                 if (not slots.get('year_from') or (r[2] and slots['year_from'] <= r[2] <= slots.get('year_to', 3000)))]
    rows = list({r['id']: r for r in rows}.values())
    trace.add('graph_query', t, backend=store.name, template='films_with+related', query=query, facts=facts, rows=len(rows))
    label = ', '.join(e['name'] for e in entities if e['type'] in ('PLACE', 'TECH', 'GENRE', 'ORG')) or 'that filter'
    if 'first' in q.lower() and rows:
        rows = sorted(rows, key=lambda r: r['year'] or 9999)
    text = f"**{len(rows)} films** linked to {label}{_years(slots)}{_span(rows, slots)}." + (f' e.g. {", ".join(r["title"] + " (" + str(r["year"]) + ")" for r in rows[:4])}.' if rows else '')
    return _films_response(con, text, rows, [], trace)


def flow_plot_search(con, store, q, entities, slots, use_llm, trace):
    people = _people(entities)
    prefilter = None
    t = time.perf_counter()
    if people or slots.get('year_from'):
        rows, query = store.films_with(people, slots.get('year_from'), slots.get('year_to'), limit=5000)
        prefilter = {r['id'] for r in rows}
        trace.add('graph_prefilter', t, query=query, candidates=len(prefilter))
    t = time.perf_counter()
    try:
        hits = retrieval.hybrid_search(con, q, 8, prefilter)
        mode = 'hybrid (dense + BM25, RRF)'
    except llm.LLMUnavailable:
        hits = retrieval.hybrid_search(con, q, 8, prefilter, mode='bm25')
        mode = 'BM25 only (embedding model unavailable)'
    trace.add('retrieval', t, mode=mode, **hits['timings'], dense_hits=hits['dense_hits'], sparse_hits=hits['sparse_hits'],
              top=[h['film_id'] for h in hits['films'][:5]])
    films = hits['films']
    if not films:
        return {'answer': 'No passage matched that description.'}
    cards = [film_card(con, f['film_id']) for f in films[:6]]
    context = '\n\n'.join(f"[{i + 1}] {c['title']} ({c['year']}): " + ' '.join(ch['text'] for ch in f['chunks'])[:900]
                          for i, (f, c) in enumerate(zip(films[:5], cards)))
    text = None
    if use_llm and llm.available():
        t = time.perf_counter()
        try:
            text, usage = llm.chat([{'role': 'system', 'content': GROUNDED},
                                    {'role': 'user', 'content': f'Question: {q}\n\nContext passages:\n{context}'}], max_tokens=260)
            trace.add('generation', t, **usage)
        except llm.LLMUnavailable as exc:
            trace.add('generation', t, error=str(exc))
    if not text:
        text = 'Best matches by story passage:\n' + '\n'.join(f"{i + 1}. **{c['title']}** ({c['year']}) — {f['chunks'][0]['text'][:140]}…"
                                                             for i, (f, c) in enumerate(zip(films[:5], cards)))
    return {'answer': text, 'cards': cards, 'graph': film_graph(con, [f['film_id'] for f in films[:8]], [p['id'] for p in people]),
            'sources': [{'n': i + 1, 'film': c['title'], 'url': f['chunks'][0]['source_url'], 'text': f['chunks'][0]['text']}
                        for i, (f, c) in enumerate(zip(films[:5], cards))]}


GROUNDED = ('You answer questions about Malayalam films using ONLY the numbered context passages. Identify the film(s) that best '
            'match the question, explain briefly why, and cite passages like [1]. If none match, say so. Never invent titles, '
            'names or years. Keep it under 120 words.')


def flow_similar_films(con, store, q, entities, slots, use_llm, trace):
    film = next(e for e in entities if e['type'] == 'FILM')
    t = time.perf_counter()
    # Graph signal: shared key creatives, weighted by role importance.
    weights = {'director': 3, 'writer': 3, 'music_director': 1.5, 'cinematographer': 1, 'actor': 1}
    scores, why = Counter(), {}
    for pid, role in con.execute("SELECT person_id, role FROM kg_credits WHERE film_id=? AND role IN ('director','writer','music_director','cinematographer','actor')", (film['id'],)):
        lead = role != 'actor' or con.execute('SELECT film_count FROM kg_people WHERE id=?', (pid,)).fetchone()[0] >= 15
        if not lead:
            continue
        for (other,) in con.execute('SELECT film_id FROM kg_credits WHERE person_id=? AND role=? AND film_id!=?', (pid, role, film['id'])):
            scores[other] += weights[role]
            why.setdefault(other, []).append(f"{con.execute('SELECT name FROM kg_people WHERE id=?', (pid,)).fetchone()[0]} ({ROLE_LABEL(role)})")
    trace.add('graph_query', t, template='shared_creatives', candidates=len(scores))
    # Semantic signal: plot similarity of the film's own passages.
    t = time.perf_counter()
    plot = con.execute("SELECT text FROM kg_chunks WHERE film_id=? ORDER BY kind='plot' DESC, seq LIMIT 2", (film['id'],)).fetchall()
    semantic = {}
    if plot:
        try:
            for rank, f in enumerate(retrieval.hybrid_search(con, ' '.join(p[0] for p in plot)[:1500], 30)['films']):
                if f['film_id'] != film['id']:
                    semantic[f['film_id']] = 1 / (60 + rank)
        except llm.LLMUnavailable:
            pass
    trace.add('retrieval', t, mode='plot similarity', candidates=len(semantic))
    graph_rank = {f: 1 / (60 + i) for i, (f, _) in enumerate(scores.most_common(60))}
    fused = sorted(set(graph_rank) | set(semantic), key=lambda f: -(graph_rank.get(f, 0) + semantic.get(f, 0)))[:8]
    cards = [film_card(con, f) for f in fused]
    lines = [f"{i + 1}. **{c['title']}** ({c['year']}) — " + (('shares ' + ', '.join(dict.fromkeys(why[f]))[:110]) if f in why else 'similar story')
             for i, (f, c) in enumerate(zip(fused, cards))]
    text = f"Films like **{film['name']}**, ranked by shared creatives (graph) fused with plot similarity (vectors):\n" + '\n'.join(lines)
    return {'answer': text, 'cards': cards, 'graph': film_graph(con, [film['id']] + fused, [])}


def flow_global_overview(con, store, q, entities, slots, use_llm, trace):
    from .communities import search_communities
    t = time.perf_counter()
    query = q + ' ' + ' '.join(e['name'] for e in entities)
    found = search_communities(con, query, 4)
    if slots.get('year_from'):
        found = sorted(found, key=lambda c: -(c['facts']['active_years'] and c['facts']['active_years'][0] <= slots.get('year_to', 3000)
                                             and c['facts']['active_years'][1] >= slots['year_from']))
    trace.add('community_search', t, communities=[c['id'] for c in found])
    if not found:
        return {'answer': 'Community summaries have not been built yet (run `filmgraph communities`).'}
    context = '\n\n'.join(f"[{c['id']}] {c['summary']}" for c in found)
    text = None
    if use_llm and llm.available():
        t = time.perf_counter()
        try:
            text, usage = llm.chat([{'role': 'system', 'content': 'Answer the question about Malayalam cinema using only these community reports. Cite reports like [c001]. Under 150 words.'},
                                    {'role': 'user', 'content': f'Question: {q}\n\nReports:\n{context}'}], max_tokens=300)
            trace.add('generation', t, **usage)
        except llm.LLMUnavailable:
            pass
    text = text or 'Relevant collaboration circles:\n' + '\n'.join(f"- **{c['title']}** [{c['id']}]: {c['summary']}" for c in found)
    members = [m['id'] for c in found[:2] for m in c['facts']['core_members'][:8]]
    films = [f['id'] for c in found[:2] for f in c['facts']['signature_films'][:5]]
    graph = film_graph(con, films, members)
    for n in graph['nodes']:
        for c in found[:2]:
            if any(m['id'] == n['id'] for m in c['facts']['core_members']):
                n['community'] = c['id']
    return {'answer': text, 'graph': graph, 'communities': [{k: c[k] for k in ('id', 'title', 'summary', 'size')} for c in found]}


def flow_help(con, store, q, entities, slots, use_llm, trace):
    return {'answer': ('I answer questions over a knowledge graph of Malayalam cinema: collaborations and counts, filmographies, '
                       'who-did-what on a film, songs, how two people are connected, remakes/adaptations/awards, films by place or '
                       'technology, a film from a half-remembered plot, similar films, and overviews of eras and teams.'),
            'suggestions': SUGGESTIONS[:6]}


def flow_out_of_scope(con, store, q, entities, slots, use_llm, trace):
    return {'answer': 'That is outside what I know. I only cover Malayalam films, their people, songs and stories.', 'suggestions': SUGGESTIONS[:3]}


def flow_handover(con, store, q, entities, slots, use_llm, trace):
    return handover(con, q, 'user request', {})


def handover(con, question, reason, context) -> dict:
    """Human-in-the-loop: queue a ticket; post to Slack if SLACK_WEBHOOK_URL is configured."""
    ticket = {'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'question': question, 'reason': reason, 'context': context}
    Path('data').mkdir(exist_ok=True)
    with open('data/handover.jsonl', 'a') as fh:
        fh.write(json.dumps(ticket, ensure_ascii=False) + '\n')
    delivered = 'queued locally (data/handover.jsonl)'
    if os.getenv('SLACK_WEBHOOK_URL'):
        try:
            req = urllib.request.Request(os.environ['SLACK_WEBHOOK_URL'], json.dumps({'text': f'FilmGraph handover ({reason}): {question}'}).encode(),
                                         {'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
            delivered = 'sent to the curator Slack channel'
        except OSError:
            pass
    return {'answer': f"I've passed this to a human curator — {delivered}. They review data corrections and questions I can't answer.",
            'flow': 'handover', 'handover': ticket}


FLOWS = {'collaboration': flow_collaboration, 'filmography': flow_filmography, 'top_collaborators': flow_top_collaborators,
         'film_info': flow_film_info, 'film_crew': flow_film_crew, 'songs': flow_songs, 'connection_path': flow_connection_path,
         'relation_query': flow_relation_query, 'by_attribute': flow_by_attribute, 'plot_search': flow_plot_search,
         'similar_films': flow_similar_films, 'global_overview': flow_global_overview, 'help': flow_help,
         'out_of_scope': flow_out_of_scope, 'handover': flow_handover}


# ---------------------------------------------------------------- cards and graphs

def _films_response(con, text, rows, focus, trace):
    t = time.perf_counter()
    cards = [film_card(con, r['id']) for r in rows[:12]]
    graph = film_graph(con, [r['id'] for r in rows[:60]], focus)
    trace.add('hydrate', t, cards=len(cards), nodes=len(graph['nodes']))
    return {'answer': text, 'cards': cards, 'graph': graph, 'total': len(rows),
            'film_ids': [r['id'] for r in rows]}


def film_card(con, film_id) -> dict:
    f = con.execute('SELECT * FROM kg_films WHERE id=?', (film_id,)).fetchone()
    if not f:
        return {'id': film_id, 'title': film_id}
    f = dict(zip([d[0] for d in con.execute('SELECT * FROM kg_films LIMIT 0').description], f))
    credits = con.execute("""SELECT p.name, c.role FROM kg_credits c JOIN kg_people p ON p.id=c.person_id WHERE c.film_id=?
                             ORDER BY c.role='director' DESC, p.film_count DESC""", (film_id,)).fetchall()
    pick = lambda role, n: [name for name, r in credits if r == role][:n]
    plot = con.execute("SELECT text FROM kg_chunks WHERE film_id=? ORDER BY kind='plot' DESC, kind='lead' DESC, seq LIMIT 1", (film_id,)).fetchone()
    raw_ids = [r[0] for r in con.execute('SELECT film_id FROM kg_film_map WHERE canonical_id=?', (film_id,))]
    videos = [r[0] for r in con.execute(f"SELECT DISTINCT value FROM facts WHERE kind IN ('youtube_url','ott_url','official_trailer') AND film_id IN ({','.join('?' * len(raw_ids))})", raw_ids)] if raw_ids else []
    rating = con.execute(f"SELECT value FROM facts WHERE kind='imdb_rating' AND film_id IN ({','.join('?' * len(raw_ids))}) LIMIT 1", raw_ids).fetchone() if raw_ids else None
    search = f"{f['title']} {f['year'] or ''} Malayalam"
    links = []
    if f['wikipedia_url']:
        links.append({'label': 'Wikipedia', 'url': f['wikipedia_url'], 'verified': True})
    if f['imdb_id']:
        links.append({'label': 'IMDb', 'url': f"https://www.imdb.com/title/{f['imdb_id']}/", 'verified': True})
    links += [{'label': 'YouTube', 'url': v, 'verified': True} for v in videos[:2]]
    links += [{'label': 'Find trailer', 'url': 'https://www.youtube.com/results?search_query=' + quote(search + ' trailer'), 'verified': False},
              {'label': 'Where to watch', 'url': 'https://www.justwatch.com/in/search?q=' + quote(f['title']), 'verified': False}]
    return {'id': film_id, 'title': f['title'], 'year': f['year'], 'directors': pick('director', 2), 'cast': pick('actor', 4),
            'music': pick('music_director', 2), 'plot': plot[0] if plot else None, 'links': links,
            'imdb_rating': rating[0] if rating else None}


def _person_node(con, pid, focus=False):
    row = con.execute('SELECT name, primary_role, wikipedia_url FROM kg_people WHERE id=?', (pid,)).fetchone()
    return {'id': pid, 'label': row[0] if row else pid, 'group': 'focus' if focus else (row[1] if row else 'actor'),
            'roles': [row[1]] if row else [], 'url': row[2] if row else None}


def film_graph(con, film_ids, focus) -> dict:
    """Result subgraph for the UI: films + focus people + key credits + linked places/entities."""
    nodes, edges, seen = {}, [], set()
    focus = set(focus)
    for fid in dict.fromkeys(film_ids):
        f = con.execute('SELECT title, year, wikipedia_url FROM kg_films WHERE id=?', (fid,)).fetchone()
        if not f:
            continue
        nodes[fid] = {'id': fid, 'label': f[0], 'year': f[1], 'group': 'film', 'url': f[2]}
        credits = con.execute("""SELECT p.id, p.name, c.role, p.film_count FROM kg_credits c JOIN kg_people p ON p.id=c.person_id
                                 WHERE c.film_id=? ORDER BY p.film_count DESC""", (fid,)).fetchall()
        chosen = [c for c in credits if c[0] in focus]
        chosen += [c for c in credits if c[0] not in focus and c[2] in ('director', 'writer', 'music_director')][:4]
        chosen += [c for c in credits if c[0] not in focus and c[2] == 'actor'][:3 if len(film_ids) > 20 else 5]
        for pid, name, role, _ in chosen:
            node = nodes.setdefault(pid, {'id': pid, 'label': name, 'group': 'focus' if pid in focus else role, 'roles': []})
            if role not in node['roles']:
                node['roles'].append(role)
            if (pid, fid, role) not in seen:
                seen.add((pid, fid, role)); edges.append({'from': pid, 'to': fid, 'label': ROLE_LABEL(role), 'role': role})
        if len(film_ids) <= 12 and con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_triples'").fetchone():
            for rel, obj, title, etype in con.execute("""SELECT t.relation, t.object_id, e.title, e.type FROM kg_triples t JOIN kg_entities e ON e.id=t.object_id
                    WHERE t.film_id=? AND t.subject_id=? AND t.relation!='MENTIONS' LIMIT 6""", (fid, fid)):
                target = (con.execute('SELECT kg_id FROM kg_entities WHERE id=?', (obj,)).fetchone() or [None])[0] or obj
                nodes.setdefault(target, {'id': target, 'label': title, 'group': 'location' if etype == 'Place' else 'entity', 'entity_type': etype})
                edges.append({'from': fid, 'to': target, 'label': ROLE_LABEL(rel.lower()), 'role': rel})
    for fid in film_ids:
        for kind, value in con.execute("SELECT kind, value FROM kg_facts WHERE film_id=? AND kind IN ('location','technology') LIMIT 3", (fid,)):
            nid = f'{kind}:{value}'
            nodes.setdefault(nid, {'id': nid, 'label': value, 'group': 'location' if kind == 'location' else 'technology'})
            edges.append({'from': fid, 'to': nid, 'label': kind, 'role': kind})
    communities = dict(con.execute('SELECT person_id, community_id FROM kg_community_members')) if \
        con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_community_members'").fetchone() else {}
    for n in nodes.values():
        if n['id'] in communities:
            n['community'] = communities[n['id']]
    return {'nodes': list(nodes.values()), 'edges': edges, 'highlight': list(focus) + list(film_ids[:12])}


def triple_graph(con, triples) -> dict:
    nodes, edges = {}, []
    for t in triples:
        nodes.setdefault(t['film_id'], {'id': t['film_id'], 'label': t['film'], 'year': t['year'], 'group': 'film'})
        sub = t['film_id'] if t['subject_id'] == t['film_id'] else t['subject_id']
        if sub not in nodes:
            nodes[sub] = {'id': sub, 'label': sub.replace('wiki:', '').replace('_', ' '), 'group': 'entity'}
        nodes.setdefault(t['object_id'], {'id': t['object_id'], 'label': t['object'], 'group': 'location' if t.get('object_type') == 'Place' else 'entity',
                                          'entity_type': t.get('object_type')})
        edges.append({'from': sub, 'to': t['object_id'], 'label': ROLE_LABEL(t['relation'].lower()), 'role': t['relation']})
    return {'nodes': list(nodes.values()), 'edges': edges, 'highlight': list(nodes)[:10]}


def _log(response):
    if os.getenv('FILMGRAPH_TRACE_LOG', '1') != '1':
        return
    try:
        with open('data/traces.jsonl', 'a') as fh:
            fh.write(json.dumps({'at': time.time(), 'question': response['question'], 'intent': response['intent'],
                                 'method': response['method'], 'confidence': response['confidence'], 'flow': response.get('flow'),
                                 'backend': response['backend'], 'total_ms': response['total_ms'],
                                 'stages': [{k: s[k] for k in ('stage', 'ms')} for s in response['trace']]}, ensure_ascii=False) + '\n')
    except OSError:
        pass
