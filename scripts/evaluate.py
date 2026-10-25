"""Offline evaluation of the GraphRAG pipeline.

Metrics
  NLU        intent accuracy per tier (rules+classifier, +LLM fallback); entity-linking P/R/F1
  answers    count exactness vs an independent ground-truth SQL written here (not via graphstore)
  crew       expected names present in film_crew answers
  retrieval  hit@1 / hit@5 / MRR on remembered-plot queries for BM25 vs vector vs hybrid (RRF)
  parity     SQLite mirror vs Neo4j return identical film sets for the same plan
  latency    p50 / p95 per configuration
Writes evals/report.json and evals/report.md.

usage: python scripts/evaluate.py [--llm] [--neo4j]
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, 'src')
from filmgraph import nlu, pipeline, retrieval  # noqa: E402
from filmgraph.graphstore import SQLiteStore, open_store  # noqa: E402


def truth_count(con, people, slots):
    """Ground truth: distinct films where every person holds one of their roles (film or song level)."""
    sets = []
    for p in people:
        roles = p['roles']
        q = 'SELECT film_id FROM kg_credits WHERE person_id=?' + (' AND role IN (%s)' % ','.join('?' * len(roles)) if roles else '')
        q2 = 'SELECT s.film_id FROM kg_song_credits c JOIN kg_songs s ON s.id=c.song_id WHERE c.person_id=?' + (' AND c.role IN (%s)' % ','.join('?' * len(roles)) if roles else '')
        films = {r[0] for r in con.execute(q, [p['id'], *roles])} | {r[0] for r in con.execute(q2, [p['id'], *roles])}
        sets.append(films)
    films = set.intersection(*sets) if sets else set()
    if slots:
        years = dict(con.execute(f"SELECT id, year FROM kg_films WHERE id IN ({','.join('?' * len(films))})", list(films))) if films else {}
        films = {f for f in films if years.get(f) and slots.get('year_from', 0) <= years[f] <= slots.get('year_to', 3000)}
    return len(films)


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def evaluate_pipeline(con, gold, use_llm):
    rows, latencies = [], []
    tp = fp = fn = 0
    for case in gold:
        t = time.perf_counter()
        out = pipeline.answer(con, case['question'], None, None, use_llm=use_llm, backend='sqlite')
        latencies.append((time.perf_counter() - t) * 1000)
        got = {e['id'] for e in out['entities'] if e['type'] == 'PERSON'}
        want = {p['id'] for p in case['people']}
        tp += len(got & want); fp += len(got - want); fn += len(want - got)
        row = {'question': case['question'], 'expected': case['intent'], 'got': out['intent'], 'method': out['method'],
               'intent_ok': out['intent'] == case['intent'], 'entities_ok': want <= got}
        if case['intent'] in ('collaboration', 'filmography') and case['people']:
            expected = truth_count(con, case['people'], case.get('slots'))
            row.update(expected_count=expected, got_count=out.get('total'), count_ok=out.get('total') == expected)
        if case.get('answer_contains'):
            row['answer_ok'] = all(name in out['answer'] for name in case['answer_contains'])
        if case.get('film'):
            row['film_ok'] = any(e['id'] == case['film'] for e in out['entities'])
        rows.append(row)
    n = len(rows)
    counted = [r for r in rows if 'count_ok' in r]
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {'cases': n, 'intent_accuracy': round(sum(r['intent_ok'] for r in rows) / n, 3),
            'entity_precision': round(precision, 3), 'entity_recall': round(recall, 3),
            'entity_f1': round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0,
            'count_exact': f"{sum(r['count_ok'] for r in counted)}/{len(counted)}",
            'crew_answers': f"{sum(r.get('answer_ok', False) for r in rows if 'answer_ok' in r)}/{sum('answer_ok' in r for r in rows)}",
            'film_linking': f"{sum(r.get('film_ok', False) for r in rows if 'film_ok' in r)}/{sum('film_ok' in r for r in rows)}",
            'methods': {m: sum(r['method'] == m for r in rows) for m in {r['method'] for r in rows}},
            'latency_ms_p50': pct(latencies, .5), 'latency_ms_p95': pct(latencies, .95),
            'failures': [r for r in rows if not r['intent_ok'] or r.get('count_ok') is False or r.get('answer_ok') is False
                         or r.get('film_ok') is False or not r['entities_ok']]}


def evaluate_retrieval(con, plots):
    report = {}
    for mode in ('bm25', 'vector', 'hybrid'):
        hits1 = hits5 = rr = 0
        lat = []
        for case in plots:
            t = time.perf_counter()
            films = [f['film_id'] for f in retrieval.hybrid_search(con, case['question'], 10, mode=mode)['films']]
            lat.append((time.perf_counter() - t) * 1000)
            rank = films.index(case['film']) + 1 if case['film'] in films else None
            hits1 += rank == 1; hits5 += bool(rank and rank <= 5); rr += 1 / rank if rank else 0
        n = len(plots)
        report[mode] = {'hit@1': round(hits1 / n, 3), 'hit@5': round(hits5 / n, 3), 'mrr': round(rr / n, 3),
                        'latency_ms_p50': pct(lat, .5)}
    return report


def evaluate_parity(con, gold):
    store = open_store(con, 'neo4j')
    lite = SQLiteStore(con)
    same, total, diffs = 0, 0, []
    for case in gold:
        if case['intent'] not in ('collaboration', 'filmography') or not case['people']:
            continue
        slots = case.get('slots') or {}
        a, _ = lite.films_with(case['people'], slots.get('year_from'), slots.get('year_to'), limit=5000)
        b, _ = store.films_with(case['people'], slots.get('year_from'), slots.get('year_to'), limit=5000)
        total += 1
        if {r['id'] for r in a} == {r['id'] for r in b}:
            same += 1
        else:
            diffs.append({'question': case['question'], 'sqlite': len(a), 'neo4j': len(b)})
    return {'identical': f'{same}/{total}', 'diffs': diffs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--llm', action='store_true', help='also evaluate with the local LLM fallback enabled')
    ap.add_argument('--neo4j', action='store_true', help='check SQLite/Neo4j parity')
    args = ap.parse_args()
    con = sqlite3.connect('data/films.sqlite3', timeout=60)
    gold = [json.loads(l) for l in open('evals/gold.jsonl')]
    plots = [json.loads(l) for l in open('evals/plot_gold.jsonl')]
    nlu.understand(con, 'warm up')  # build gazetteer + classifier outside the timings
    report = {'dataset': {'structured_cases': len(gold), 'plot_cases': len(plots)},
              'pipeline_rules_classifier': evaluate_pipeline(con, gold, use_llm=False)}
    if args.llm:
        report['pipeline_with_llm_fallback'] = evaluate_pipeline(con, gold, use_llm=True)
    report['plot_retrieval'] = evaluate_retrieval(con, plots)
    if args.neo4j:
        report['backend_parity'] = evaluate_parity(con, gold)
    from sklearn.base import clone
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    X, y = nlu.IntentClassifier.dataset()
    cv = cross_val_score(clone(nlu.IntentClassifier.train()), X, y, cv=StratifiedKFold(5, shuffle=True, random_state=0))
    report['classifier_cv_accuracy_synthetic'] = round(float(cv.mean()), 3)
    json.dump(report, open('evals/report.json', 'w'), indent=2, ensure_ascii=False)
    write_markdown(report)
    print(json.dumps({k: v for k, v in report.items()}, indent=1, ensure_ascii=False, default=str)[:6000])


def write_markdown(r):
    lines = ['# Evaluation report', '', f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Gold: {r['dataset']['structured_cases']} structured questions, "
             f"{r['dataset']['plot_cases']} remembered-plot questions (`evals/`).", '',
             '| Configuration | Intent acc. | Entity F1 | Count exact | Crew answers | Film linking | p50 ms | p95 ms |', '|---|---|---|---|---|---|---|---|']
    for key, label in (('pipeline_rules_classifier', 'Rules + classifier (no LLM)'), ('pipeline_with_llm_fallback', '+ local LLM fallback')):
        if key in r:
            x = r[key]
            lines.append(f"| {label} | {x['intent_accuracy']:.0%} | {x['entity_f1']:.2f} | {x['count_exact']} | {x['crew_answers']} | {x['film_linking']} | {x['latency_ms_p50']} | {x['latency_ms_p95']} |")
    lines += ['', '## Plot retrieval ablation', '', '| Retriever | hit@1 | hit@5 | MRR | p50 ms |', '|---|---|---|---|---|']
    for mode, x in r['plot_retrieval'].items():
        lines.append(f"| {mode} | {x['hit@1']:.0%} | {x['hit@5']:.0%} | {x['mrr']:.2f} | {x['latency_ms_p50']} |")
    if 'backend_parity' in r:
        lines += ['', f"## Backend parity\n\nSQLite mirror vs Neo4j identical film sets: **{r['backend_parity']['identical']}**"]
    fails = r['pipeline_rules_classifier']['failures']
    if fails:
        lines += ['', '## Failures (rules + classifier)', '']
        lines += [f"- `{f['question']}` → {f['got']} ({f['method']}); expected {f['expected']}"
                  + (f"; count {f.get('got_count')} vs truth {f.get('expected_count')}" if f.get('count_ok') is False else '')
                  + ('; missing entity' if not f['entities_ok'] else '') for f in fails]
    open('evals/report.md', 'w').write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
