"""IMDb official noncommercial TSV adapter; never scrapes IMDb web pages.

Only enriches title IDs already linked to Malayalam records. AKAs language tags
are not a reliable original-language classifier, so we do not invent a full
Malayalam catalogue from them. Full credits and plots are absent from these files.
"""
from __future__ import annotations
import csv
import gzip
import io
import json
import ssl
from pathlib import Path
from urllib.request import Request,urlopen
from .db import import_records
from .http import Client,DEFAULT_AGENT
from .catalogue import mark_job

BASE='https://datasets.imdbws.com/'
LICENSE='IMDb noncommercial datasets; personal/noncommercial use; https://www.imdb.com/interfaces/'


def rows(name,directory,client):
    """Read local licensed files when supplied, otherwise stream the official gzip."""
    local=Path(directory)/(name+'.tsv.gz')
    if local.exists():
        with gzip.open(local,'rt',encoding='utf-8',newline='') as handle:
            yield from csv.DictReader(handle,delimiter='\t')
    else:
        request=Request(BASE+name+'.tsv.gz',headers={'User-Agent':DEFAULT_AGENT})
        with urlopen(request,timeout=120,context=client.context) as response:
            with gzip.GzipFile(fileobj=response) as compressed:
                with io.TextIOWrapper(compressed,encoding='utf-8',newline='') as handle:
                    yield from csv.DictReader(handle,delimiter='\t')


def selected(name,field,ids,directory,client):
    dest=Path(directory)/('selected-'+name+'.json.gz')
    signature=','.join(sorted(ids))
    import hashlib
    signature=hashlib.sha256(signature.encode()).hexdigest()
    if dest.exists():
        cached=json.loads(gzip.decompress(dest.read_bytes()))
        if cached['signature']==signature:return cached['rows']
    print(f'IMDb streaming {name}; filtering {len(ids)} exact IDs',flush=True)
    result=[row for row in rows(name,directory,client) if row[field] in ids]
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_bytes(gzip.compress(json.dumps({'signature':signature,'rows':result}).encode()))
    print(f'IMDb {name}: retained {len(result)} rows',flush=True)
    return result


def import_imdb(con,directory=Path('data/imdb'),with_principals=True):
    client=Client();directory=Path(directory)
    films={r['imdb_id']:dict(r) for r in con.execute('SELECT * FROM films WHERE imdb_id IS NOT NULL')}
    if not films:return 0
    records={iid:{'id':f['id'],'title':f['title'],'year':f['year'],'imdb_id':iid,'facts':[],'credits':[]} for iid,f in films.items()}
    ids=set(films)
    for row in selected('title.ratings','tconst',ids,directory,client):
        rec=records[row['tconst']];source='https://www.imdb.com/title/'+row['tconst']+'/'
        for field,kind in [('averageRating','imdb_rating'),('numVotes','imdb_votes')]:
            rec['facts'].append({'kind':kind,'value':row[field],'source_url':source,'evidence':BASE+'title.ratings.tsv.gz'})
    # Commit ratings independently so an interrupted larger feed retains progress.
    import_records(con,list(records.values()))
    pending=[];people_ids=set()
    for row in selected('title.crew','tconst',ids,directory,client):
        for field,role in [('directors','director'),('writers','writer')]:
            for pid in row[field].split(','):
                if pid!='\\N':pending.append((row['tconst'],pid,role,''));people_ids.add(pid)
    if with_principals:
        roles={'actor':'actor','actress':'actor','director':'director','writer':'writer','producer':'producer',
               'composer':'composer','cinematographer':'cinematographer','editor':'editor','production_designer':'production_designer','self':'self'}
        for row in selected('title.principals','tconst',ids,directory,client):
            role=roles.get(row['category'])
            if not role:continue
            characters=row.get('characters','')
            try:characters=', '.join(json.loads(characters)) if characters!='\\N' else ''
            except (ValueError,TypeError):characters=''
            pid=row['nconst'];pending.append((row['tconst'],pid,role,characters));people_ids.add(pid)
    names={r['nconst']:r for r in selected('name.basics','nconst',people_ids,directory,client)}
    for iid,pid,role,character in pending:
        if pid not in names:continue
        records[iid]['credits'].append({'person':{'id':'imdb:'+pid,'name':names[pid]['primaryName']},
                                       'role':role,'character':character,'source_url':'https://www.imdb.com/title/'+iid+'/'})
    for iid,record in records.items():
        record['facts'].append({'kind':'source_license','value':LICENSE,'source_url':'https://www.imdb.com/title/'+iid+'/'})
    count=import_records(con,list(records.values()))
    mark_job(con,'imdb','official noncommercial datasets','complete')
    return count
