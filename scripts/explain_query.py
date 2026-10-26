"""Walk one question through the real pipeline, stage by stage, printing the function that ran
(file:line), its inputs and its intermediate output. Writes a Markdown walkthrough.

usage: NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=filmgraph-local \
       python scripts/explain_query.py "mohanlal and mammootty movies" [--out docs/walkthrough.md]
"""
import argparse
import inspect
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, 'src')
from filmgraph import nlu, pipeline  # noqa: E402
from filmgraph.graphstore import open_store  # noqa: E402

out: list[str] = []


def where(fn):
    file = os.path.relpath(inspect.getsourcefile(fn))
    return f'{file}:{inspect.getsourcelines(fn)[1]}'


def step(n, title, fn, note=''):
    out.append(f'\n## {n}. {title}\n\n`{fn.__qualname__}` — `{where(fn)}`' + (f'\n\n{note}' if note else ''))


def block(obj, lang='json'):
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    out.append(f'\n```{lang}\n{text}\n```')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('question')
    ap.add_argument('--out', default='docs/walkthrough.md')
    args = ap.parse_args()
    q = args.question
    con = sqlite3.connect('data/films.sqlite3', timeout=60)
    gaz = nlu.gazetteer(con)
    out.append(f'# Walkthrough: “{q}”\n\nGenerated {time.strftime("%Y-%m-%d %H:%M")} by `scripts/explain_query.py` against the live data.')

    step(1, 'Normalise + gazetteer NER (longest match, nicknames, fuzzy repair)', nlu.Gazetteer.tag,
         'Every n-gram is looked up in a dictionary of 38k KG names, aliases, titles, places and curated nicknames.')
    spans = gaz.tag(q)
    block({'normalised': nlu.normalize(q), 'spans': [{'text': s['text'], 'corrected_to': s.get('corrected_to'),
           'candidates': [{k: c[k] for k in ('type', 'id', 'name', 'weight')} for c in s['candidates'][:3]]} for s in spans]})

    step(2, 'Entity linking: one KG id per mention', nlu.link, 'Exact names beat stray aliases; comparable candidates become a clarification.')
    linked = nlu.link(spans, q)
    block({'entities': [{k: e.get(k) for k in ('type', 'id', 'name', 'text')} for e in linked['entities']], 'ambiguous': linked['ambiguous']})

    step(3, 'Role and time slots', nlu.assign_roles)
    nlu.assign_roles(q, linked['entities'])
    block({'roles': {e['name']: e.get('roles') for e in linked['entities']}, 'time_slots': nlu.time_slots(q)})

    step(4, 'Delexicalise + intent routing (rules → classifier → LLM)', nlu.understand)
    result = nlu.understand(con, q, use_llm=False)
    block({'delexicalised': result['trace']['delexicalised'], 'rule_fired': result['trace'].get('rule'),
           'classifier_top3': result['trace']['classifier_top3'], 'intent': result['intent'], 'method': result['method'],
           'confidence': result['confidence']})

    store = open_store(con)
    people = [{'id': e['id'], 'name': e['name'], 'roles': e.get('roles') or []} for e in result['entities'] if e['type'] == 'PERSON']
    trace = pipeline.Trace()
    step(5, 'Default roles (people who are actors → films they both ACTED in)', pipeline.default_roles)
    before = json.loads(json.dumps(people))
    pipeline.default_roles(con, people, trace)
    block({'before': before, 'after': people, 'trace': trace.steps})

    step(6, f'Parameterised graph query on {store.name}', store.films_with,
         'Only validated ids, whitelisted relationship types and integers reach the query; user text never does.')
    t = time.perf_counter()
    rows, query = store.films_with(people)
    ms = round((time.perf_counter() - t) * 1000, 1)
    block(query, 'cypher' if store.name == 'neo4j' else 'sql')
    block({'params': [p['id'] for p in people], 'rows': len(rows), 'ms': ms, 'first_rows': rows[:5]})

    step(7, 'Provenance per edge: is each credit on the film’s own Wikipedia page?', pipeline.evidence)
    for r in rows:
        r['evidence'] = pipeline.evidence(con, r['id'], people)
    confirmed = [r for r in rows if r['evidence']['confirmed']]
    weak = [r for r in rows if not r['evidence']['confirmed']]
    block({'confirmed': len(confirmed), 'weak': len(weak),
           'weak_examples': [{'film': f"{r['title']} ({r['year']})", 'evidence': r['evidence']['people']} for r in weak[:6]],
           'confirmed_example': confirmed[0] if confirmed else None})

    step(8, 'Full pipeline answer (cards + graph + trace)', pipeline.answer)
    resp = pipeline.answer(con, q, use_llm=False)
    out.append('\n**Answer shown in chat:**\n\n> ' + resp['answer'].replace('\n', '\n> '))
    block({'flow': resp.get('flow'), 'backend': resp['backend'], 'total_ms': resp['total_ms'],
           'cards': [f"{c['title']} ({c['year']}) — {'✓' if (c.get('evidence') or {}).get('confirmed') else '⚠'}" for c in resp['cards'][:8]],
           'graph': {'nodes': len(resp['graph']['nodes']), 'edges': len(resp['graph']['edges']),
                     'dashed_weak_edges': sum(bool(e.get('dashes')) for e in resp['graph']['edges'])},
           'trace': [{k: v for k, v in s.items() if k not in ('query', 'entities', 'classifier_top3', 'delexicalised')} for s in resp['trace']]})
    open(args.out, 'w').write('\n'.join(out) + '\n')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
