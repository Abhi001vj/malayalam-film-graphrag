"""Rebuild the derived GraphRAG layers in order (all resumable/idempotent)."""
import json, sys, sqlite3, time
sys.path.insert(0, 'src')
from filmgraph.kg import build_kg
from filmgraph import retrieval, extract
steps = sys.argv[1:] or ['kg', 'chunks', 'mentions', 'types', 'rules', 'embeddings']
con = sqlite3.connect('data/films.sqlite3', timeout=60)
for step in steps:
    t = time.time()
    if step == 'kg': out = build_kg(con)
    elif step == 'chunks': out = retrieval.build_chunks(con)
    elif step == 'mentions': out = extract.extract_mentions(con)
    elif step == 'types': out = extract.type_entities(con)
    elif step == 'rules': out = extract.apply_rules(con)
    elif step == 'embeddings': out = retrieval.build_embeddings(con)
    elif step.startswith('llm'): out = extract.llm_label(con, int(step[3:] or 200))
    print(step, json.dumps(out, default=str), f'{time.time()-t:.0f}s', flush=True)
