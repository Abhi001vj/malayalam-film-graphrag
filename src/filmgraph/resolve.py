"""Resolve Wikipedia redirects/QIDs without merging on a guessed name match."""
from __future__ import annotations
from urllib.parse import urlencode,unquote
from .db import upsert_person
from .catalogue import wiki_identity,page_url,mark_job


def resolve_people(con,client):
    sources={r['id']:dict(r) for r in con.execute("SELECT id,name FROM people WHERE id LIKE 'wiki:%'")}
    done={r[0] for r in con.execute("SELECT item FROM import_jobs WHERE source='person_identity' AND status='complete'")}
    sources={k:v for k,v in sources.items() if k not in done}
    ids=sorted(sources);count=0
    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        titles=[unquote(pid[5:]).replace('_',' ') for pid in batch]
        params={'action':'query','prop':'pageprops','ppprop':'wikibase_item','redirects':1,'format':'json','formatversion':2,'titles':'|'.join(titles)}
        try:
            data=client.json('https://en.wikipedia.org/w/api.php?'+urlencode(params)).get('query',{})
            redirects={r['from']:r['to'] for r in [*data.get('normalized',[]),*data.get('redirects',[])]}
            pages={p['title']:p for p in data.get('pages',[])}
            for pid,title in zip(batch,titles):
                visited=set()
                while title in redirects and title not in visited:visited.add(title);title=redirects[title]
                page=pages.get(title,{})
                if page.get('missing') is not None or not page:continue
                qid=page.get('pageprops',{}).get('wikibase_item')
                canonical=wiki_identity(page_url(title))
                name=title.rsplit(' (',1)[0]
                upsert_person(con,{'id':qid or canonical,'name':name,'identifiers':[pid,canonical],
                                   'aliases':[sources[pid]['name'],title]})
                mark_job(con,'person_identity',pid,'complete');count+=1
            con.commit()
            print(f'Person identities {min(start+50,len(ids))}/{len(ids)}; resolved {count}',flush=True)
        except Exception as exc:
            con.rollback();print(f'Person resolution failed: {exc}',flush=True)
    return count


def preview_film_identities(con,client,output):
    """Read-only plan: save exact page/QID links for review; do not change films."""
    import json
    from pathlib import Path
    films=[dict(r) for r in con.execute('SELECT * FROM films WHERE wikipedia_url IS NOT NULL ORDER BY id')]
    proposals=[];destination=Path(output);destination.parent.mkdir(parents=True,exist_ok=True)
    for start in range(0,len(films),50):
        batch=films[start:start+50]
        titles=[unquote(f['wikipedia_url'].split('/wiki/')[-1]).replace('_',' ') for f in batch]
        params={'action':'query','prop':'pageprops','ppprop':'wikibase_item','redirects':1,'format':'json','formatversion':2,'titles':'|'.join(titles)}
        try:
            data=client.json('https://en.wikipedia.org/w/api.php?'+urlencode(params)).get('query',{})
            redirects={r['from']:r['to'] for r in [*data.get('normalized',[]),*data.get('redirects',[])]}
            pages={p['title']:p for p in data.get('pages',[])}
            for film,title in zip(batch,titles):
                visited=set()
                while title in redirects and title not in visited:visited.add(title);title=redirects[title]
                page=pages.get(title,{})
                if page.get('missing') is not None or not page:continue
                qid=page.get('pageprops',{}).get('wikibase_item')
                if not qid:continue
                proposals.append({'old_id':film['id'],'title':film['title'],'year':film['year'],
                                  'old_url':film['wikipedia_url'],'canonical_url':page_url(title),'wikidata_id':qid})
            destination.write_text(json.dumps(proposals,ensure_ascii=False,indent=2))
            print(f'Film identity preview {min(start+50,len(films))}/{len(films)}; exact links {len(proposals)}',flush=True)
        except Exception as exc:print(f'Identity preview request failed: {exc}',flush=True)
    return proposals
