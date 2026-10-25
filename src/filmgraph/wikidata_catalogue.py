"""Unbounded catalogue discovery and resumable batches of structured Wikidata claims."""
from __future__ import annotations
import re
from urllib.parse import urlencode, unquote
from .catalogue import mark_job, wiki_identity, page_url
from .db import import_records
from .http import Client

ROLES = {'P161':'actor','P57':'director','P162':'producer','P58':'writer',
         'P86':'composer','P1040':'editor','P344':'cinematographer','P676':'lyricist',
         'P175':'performer','P2554':'production_designer','P5028':'sound_designer',
         'P5126':'assistant_director','P1809':'choreographer','P2515':'costume_designer',
         'P4805':'makeup_artist','P3174':'art_director','P1431':'executive_producer'}
FACTS = {'P915':'filming_location','P840':'narrative_location','P136':'genre',
         'P462':'color','P2061':'aspect_ratio','P437':'distribution_format','P3803':'film_format'}


def sparql(client, query):
    return client.json('https://query.wikidata.org/sparql?'+urlencode({'query':query,'format':'json'}))['results']['bindings']


def value(row,key,default=None):
    return row.get(key,{}).get('value',default)


def discover(con,client):
    # No date requirement: films missing release dates are still discoverable.
    query='''SELECT DISTINCT ?film ?filmLabel ?ml ?date ?article ?imdb WHERE {
      ?film wdt:P364 wd:Q36236; wdt:P31/wdt:P279* wd:Q11424.
      OPTIONAL {?film rdfs:label ?filmLabel FILTER(LANG(?filmLabel)="en")}
      OPTIONAL {?film rdfs:label ?ml FILTER(LANG(?ml)="ml")}
      OPTIONAL {?film wdt:P577 ?date}
      OPTIONAL {?film wdt:P345 ?imdb}
      OPTIONAL {?article schema:about ?film; schema:isPartOf <https://en.wikipedia.org/>}
    }'''
    rows=sparql(client,query)
    records={}
    for row in rows:
        qid=value(row,'film').rsplit('/',1)[-1]
        year=re.search(r'\d{4}',value(row,'date',''))
        if qid not in records:
            records[qid]={'id':qid,'title':value(row,'filmLabel',value(row,'ml',qid)),
                          'year':int(year[0]) if year else None,'wikipedia_url':value(row,'article'),
                          'imdb_id':value(row,'imdb'),'facts':[], 'credits':[], 'documents':[]}
        record=records[qid]
        source='https://www.wikidata.org/wiki/'+qid
        record['facts'].append({'kind':'wikidata_id','value':qid,'source_url':source})
        if year:
            y=int(year[0])
            record['year']=min(record['year'],y) if record['year'] else y
            record['facts'].append({'kind':'release_date','value':value(row,'date'),'source_url':source})
        if value(row,'ml'): record['facts'].append({'kind':'title_alias','value':value(row,'ml'),'source_url':source})
    count=import_records(con,list(records.values()))
    mark_job(con,'wikidata_discovery','Malayalam films','complete')
    print(f'Wikidata discovery: {count} film entities',flush=True)
    return count


def enrich(con,client,batch_size=80):
    films=con.execute("SELECT DISTINCT f.*,x.value qid FROM films f JOIN facts x ON x.film_id=f.id AND x.kind='wikidata_id' "
                      "WHERE NOT EXISTS(SELECT 1 FROM import_jobs j WHERE j.source='wikidata_entity' AND j.item=x.value AND j.status='complete') ORDER BY x.value").fetchall()
    count=0
    for start in range(0,len(films),batch_size):
        batch=films[start:start+batch_size]
        ids=' '.join('wd:'+f['qid'] for f in batch)
        props=' '.join('wdt:'+p for p in [*ROLES,*FACTS])
        query=f'''SELECT DISTINCT ?film ?property ?item ?itemLabel ?ml ?article ?imdb WHERE {{
          VALUES ?film {{ {ids} }} VALUES ?property {{ {props} }}
          ?film ?property ?item.
          OPTIONAL {{?item rdfs:label ?itemLabel FILTER(LANG(?itemLabel)="en")}}
          OPTIONAL {{?item rdfs:label ?ml FILTER(LANG(?ml)="ml")}}
          OPTIONAL {{?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/>}}
          OPTIONAL {{?item wdt:P345 ?imdb}}
        }}'''
        records={f['qid']:{'id':f['id'],'title':f['title'],'year':f['year'],'facts':[],'credits':[]} for f in batch}
        try:
            rows=sparql(client,query)
            for row in rows:
                fid=value(row,'film').rsplit('/',1)[-1];prop=value(row,'property').rsplit('/',1)[-1]
                item=value(row,'item');label=value(row,'itemLabel',item.rsplit('/',1)[-1])
                source='https://www.wikidata.org/wiki/'+fid
                if prop in ROLES:
                    if not re.search(r'/Q\d+$',item): continue
                    person={'id':item.rsplit('/',1)[-1],'name':label,'aliases':[],'identifiers':[]}
                    if value(row,'article'): person['identifiers'].append(wiki_identity(value(row,'article')))
                    if value(row,'imdb'): person['identifiers'].append('imdb:'+value(row,'imdb'))
                    if value(row,'ml'): person['aliases'].append(value(row,'ml'))
                    records[fid]['credits'].append({'person':person,'role':ROLES[prop],'source_url':source})
                elif prop in FACTS:
                    records[fid]['facts'].append({'kind':FACTS[prop],'value':label,'source_url':source,'evidence':item})
            import_records(con,list(records.values()))
            for film in batch:mark_job(con,'wikidata_entity',film['qid'],'complete')
            count+=len(batch)
            print(f'Wikidata credits and locations: {min(start+batch_size,len(films))}/{len(films)}',flush=True)
        except Exception as exc:
            for film in batch:mark_job(con,'wikidata_entity',film['qid'],'failed',str(exc))
            print(f'Wikidata batch failed: {exc}',flush=True)
    return count
