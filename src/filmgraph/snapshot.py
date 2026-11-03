"""Create a compact serving snapshot, excluding noncommercial IMDb claims by default."""
from __future__ import annotations
import gzip
import json
import sqlite3
import tempfile
from pathlib import Path
from .db import connect,normalize
from urllib.parse import unquote
from .search import coverage


def snapshot(con, destination=Path('data/deploy.sqlite3.gz'),include_imdb=False):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'catalogue.sqlite3'
        target=sqlite3.connect(path);con.backup(target);target.close()
        with connect(path) as target:
            if not include_imdb:
                # A credit that Wikipedia/Wikidata also supports survives, re-attributed to that public source.
                imdb="(source_url LIKE 'https://www.imdb.com/%' OR source_url LIKE 'https://datasets.imdbws.com/%')"
                target.execute(f"""UPDATE credits SET source_url=(SELECT cs.source_url FROM credit_sources cs WHERE cs.film_id=credits.film_id
                    AND cs.person_id=credits.person_id AND cs.role=credits.role AND cs.character_name=credits.character_name
                    AND NOT (cs.source_url LIKE 'https://www.imdb.com/%' OR cs.source_url LIKE 'https://datasets.imdbws.com/%') LIMIT 1)
                    WHERE {imdb} AND EXISTS(SELECT 1 FROM credit_sources cs WHERE cs.film_id=credits.film_id AND cs.person_id=credits.person_id
                    AND cs.role=credits.role AND cs.character_name=credits.character_name
                    AND NOT (cs.source_url LIKE 'https://www.imdb.com/%' OR cs.source_url LIKE 'https://datasets.imdbws.com/%'))""")
                for table in ('credits','credit_sources','facts','documents','song_credits'):
                    target.execute(f"DELETE FROM {table} WHERE source_url LIKE 'https://www.imdb.com/%' OR source_url LIKE 'https://datasets.imdbws.com/%'")
                target.execute("DELETE FROM import_jobs WHERE source='imdb'")
                target.execute("DELETE FROM aliases WHERE person_id IN (SELECT p.id FROM people p WHERE NOT EXISTS(SELECT 1 FROM credits c WHERE c.person_id=p.id) AND NOT EXISTS(SELECT 1 FROM song_credits c WHERE c.person_id=p.id))")
                target.execute("DELETE FROM person_identifiers WHERE person_id IN (SELECT p.id FROM people p WHERE NOT EXISTS(SELECT 1 FROM credits c WHERE c.person_id=p.id) AND NOT EXISTS(SELECT 1 FROM song_credits c WHERE c.person_id=p.id))")
                target.execute("DELETE FROM people WHERE NOT EXISTS(SELECT 1 FROM credits c WHERE c.person_id=people.id) AND NOT EXISTS(SELECT 1 FROM song_credits c WHERE c.person_id=people.id)")
                # Cross-source merges can otherwise leave IMDb-derived name variants.
                # Rebuild serving aliases from cached public Wikidata labels and Wiki titles.
                public_names={}
                for cached in Path('data/cache/http').glob('*.gz'):
                    raw=gzip.decompress(cached.read_bytes())
                    if not raw.lstrip().startswith(b'{'):continue
                    try:payload=json.loads(raw)
                    except ValueError:continue
                    for row in payload.get('results',{}).get('bindings',[]):
                        entity=row.get('item',{}).get('value','').rsplit('/',1)[-1]
                        label=row.get('itemLabel',{}).get('value')
                        if entity.startswith('Q') and label:
                            public_names.setdefault(entity,set()).add(label)
                            if row.get('ml',{}).get('value'):public_names[entity].add(row['ml']['value'])
                old_aliases={}
                for row in target.execute('SELECT person_id,alias FROM aliases'):
                    old_aliases.setdefault(row['person_id'],[]).append(row['alias'])
                target.execute('DELETE FROM aliases')
                for person in target.execute('SELECT id,name FROM people').fetchall():
                    pid=person['id'];identifiers=[pid,*[r[0] for r in target.execute('SELECT identifier FROM person_identifiers WHERE person_id=?',(pid,))]]
                    names=set()
                    for identifier in identifiers:
                        names.update(public_names.get(identifier,set()))
                        if identifier.startswith('wiki:'):
                            title=unquote(identifier[5:]).replace('_',' ')
                            names.add(title);names.add(title.rsplit(' (',1)[0])
                    # Every non-IMDb identity keeps its current name (from Wikipedia/Wikidata) as an alias.
                    if not pid.startswith('imdb:'):names.add(person['name'])
                    if pid.startswith(('demo:','unresolved:')):names.update(old_aliases.get(pid,[]))
                    if not names:continue
                    # Only IMDb-keyed people are renamed to a public name. Renaming anyone else, e.g. to a page title
                    # ("Priyadarsan") or the shortest Wikidata label ("Ousepachan", "MG Sreekumar"), breaks the
                    # same-name merge in build_kg, splits one person into several and loses the usual spelling.
                    english=sorted((n for n in names if n.isascii() and ' (' not in n),key=lambda n:(len(n),n))
                    if english and pid.startswith('imdb:'):target.execute('UPDATE people SET name=? WHERE id=?',(english[0],pid))
                    for name in names:
                        target.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?)',(pid,name,normalize(name)))
            target.commit()
            # Derived GraphRAG layers are rebuilt from the cleaned claims, never copied from the full local DB.
            from .kg import build_kg
            from .retrieval import build_chunks
            from .communities import detect
            report_kg=build_kg(target)
            # Role / cameo / self / voice / archive per acting credit. Without it, "X and Y acted together" counts
            # narration and cameos (e.g. Barroz, Pathonpatham Noottandu) as shared screen roles.
            from .appearance import build_appearances
            build_appearances(target,log=lambda *a,**k:None)
            build_chunks(target)
            if target.execute("SELECT 1 FROM sqlite_master WHERE name='kg_entities'").fetchone():
                target.execute("UPDATE kg_entities SET kg_id=NULL")
                target.execute("UPDATE kg_entities SET kg_id=(SELECT canonical_id FROM kg_person_map m WHERE m.person_id=kg_entities.id) WHERE type='Person'")
                target.execute("UPDATE kg_entities SET kg_id=(SELECT canonical_id FROM kg_film_map m WHERE m.film_id=kg_entities.id) WHERE type='Film'")
                target.execute("DELETE FROM kg_llm_done")
            detect(target)  # template community reports; LLM reports stay local
            target.execute("DROP TABLE IF EXISTS kg_mentions")  # sentence-level working table, not needed to serve
            target.commit()
            report=coverage(target)
            report['kg']=report_kg
            target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            target.execute('VACUUM')
        destination.write_bytes(gzip.compress(path.read_bytes(),compresslevel=6))
    report['imdb_dataset_claims_included']=include_imdb
    report['licensing']='Wikipedia text: CC BY-SA 4.0 with source revision URLs. Wikidata: CC0. External IMDb identifiers/links may originate from Wikimedia; IMDb dataset claims are excluded unless explicitly requested.'
    destination.with_suffix('.coverage.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return {'path':str(destination),'bytes':destination.stat().st_size,'films':report['films'],'imdb_dataset_claims_included':include_imdb}
