"""An explainable graph-overlap baseline; no LLM or third-party profiling."""
from __future__ import annotations
from .search import film_detail


def recommendations(con,seed_ids=None,limit=12):
    seed_ids=list(dict.fromkeys(seed_ids or []))[:50]
    existing=[]
    for fid in seed_ids:
        if isinstance(fid,str) and len(fid)<=300 and con.execute('SELECT 1 FROM films WHERE id=?',(fid,)).fetchone():existing.append(fid)
    scores={};reasons={}
    if existing:
        placeholders=','.join('?' for _ in existing)
        people=con.execute(f"SELECT DISTINCT person_id FROM credits WHERE film_id IN ({placeholders}) AND role IN ('actor','director','writer','screenwriter','composer')",existing).fetchall()
        ids=[p[0] for p in people]
        if ids:
            sql='SELECT c.film_id,p.name,c.role FROM credits c JOIN people p ON p.id=c.person_id WHERE c.person_id IN ('+','.join('?' for _ in ids)+')'
            seen=set()
            for row in con.execute(sql,ids):
                fid=row['film_id'];key=(fid,row['name'],row['role'])
                if fid in existing or key in seen:continue
                seen.add(key)
                weight=4 if row['role'] in {'director','writer','screenwriter'} else 2 if row['role']=='actor' else 1
                scores[fid]=scores.get(fid,0)+weight
                reasons.setdefault(fid,[]).append(row['name']+' · '+row['role'])
    if scores:
        chosen=sorted(scores,key=lambda fid:(-scores[fid],fid))[:limit]
    else:
        chosen=[r[0] for r in con.execute('SELECT id FROM films WHERE year IS NOT NULL AND year<=CAST(strftime(\'%Y\',\'now\') AS INTEGER) ORDER BY year DESC,title LIMIT ?',(limit,))]
    films=[]
    for fid in chosen:
        film=film_detail(con,fid);film['recommendation_reason']='Shared credits: '+', '.join(reasons[fid][:4]) if fid in reasons else 'Recent title in the catalogue'
        films.append(film)
    return {'personalized':bool(existing and scores),'results':films,'note':'Graph-overlap recommendations from catalogue credits. Current cinema runs and OTT availability are not verified; use the linked streaming search.'}
