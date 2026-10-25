"""Apply a reviewed exact-identity plan only to an isolated, verified backup copy."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from filmgraph.db import connect,import_records

p=argparse.ArgumentParser();p.add_argument('--staging',required=True,type=Path);p.add_argument('--plan',required=True,type=Path);args=p.parse_args()
if 'backups' not in args.staging.parts or args.staging.name=='films.sqlite3':raise SystemExit('This tool only accepts an isolated file under data/backups')
plan=json.loads(args.plan.read_text());report={'proposals':len(plan)}
with connect(args.staging) as con:
 before_sources=[tuple(r) for r in con.execute('select film_id,person_id,role,character_name,source_url from credit_sources')]
 before_docs={(r['kind'],hashlib.sha256(r['text'].encode()).hexdigest(),r['source_url']) for r in con.execute('select kind,text,source_url from documents')}
 report['before_films']=con.execute('select count(*) from films').fetchone()[0]
 for row in plan:
  original=con.execute('select * from films where id=?',(row['old_id'],)).fetchone()
  if not original:continue
  facts=[{'kind':'wikidata_id','value':row['wikidata_id'],'source_url':row['canonical_url']},
         {'kind':'canonical_wikipedia_url','value':row['canonical_url'],'source_url':row['canonical_url']}]
  import_records(con,[{**dict(original),'id':row['wikidata_id'],'wikipedia_url':row['canonical_url'],
                       'identifiers':[row['old_id']],'facts':facts}])
 report['after_films']=con.execute('select count(*) from films').fetchone()[0]
 mapping={r[0]:r[1] for r in con.execute('select identifier,film_id from film_identifiers')}
 after_sources={tuple(r) for r in con.execute('select film_id,person_id,role,character_name,source_url from credit_sources')}
 missing=[r for r in before_sources if (mapping.get(r[0],r[0]),*r[1:]) not in after_sources]
 after_docs={(r['kind'],hashlib.sha256(r['text'].encode()).hexdigest(),r['source_url']) for r in con.execute('select kind,text,source_url from documents')}
 report.update(missing_credit_source_claims=len(missing),missing_document_variants=len(before_docs-after_docs),
               foreign_key_errors=len(con.execute('PRAGMA foreign_key_check').fetchall()),integrity=con.execute('PRAGMA integrity_check').fetchone()[0])
 args.staging.with_suffix('.validation.json').write_text(json.dumps(report,indent=2))
 print(json.dumps(report,indent=2))
 if missing or before_docs-after_docs or report['foreign_key_errors'] or report['integrity']!='ok':raise SystemExit('Validation failed; do not promote this copy')
