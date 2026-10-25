"""Source-grounded natural-language collaboration queries and bounded result graphs."""
from __future__ import annotations
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import quote
from .db import normalize

STOP = set('a an and are acted actor actors about based by can director film films find for from had has have i in is it me movie movies of on or plot show story that the there this was were where which who with year how many times did do does work works worked together collaborate collaborated collaboration between starring featuring list all give tell as both also malayalam cinema please than after before since to until during through'.split())
ROLE_WORDS = {'acted':'actor','actor':'actor','actors':'actor','starred':'actor','starring':'actor',
 'directed':'director','director':'director','produced':'producer','producer':'producer',
 'wrote':'writer','writer':'writer','written':'writer','screenwriter':'screenwriter',
 'music':'composer','composer':'composer','composed':'composer','edited':'editor','editor':'editor',
 'singer':'singer','sang':'singer','sung':'singer','lyricist':'lyricist','lyrics':'lyricist',
 'cinematographer':'cinematographer','cinematography':'cinematographer','choreographer':'choreographer',
 'choreographed':'choreographer','assistant director':'assistant_director','songwriter':'lyricist'}
ROLE_GROUPS = {'writer':['writer','screenwriter','story_writer'],
 'composer':['composer','score_composer'], 'singer':['singer','performer']}


def parse_question(con: sqlite3.Connection, question: str) -> dict:
    clean = normalize(question)
    years = [int(x) for x in re.findall(r'\b(?:19|20)\d{2}\b',clean)]
    aliases = con.execute('SELECT a.person_id,a.normalized,p.name FROM aliases a JOIN people p ON p.id=a.person_id WHERE length(a.normalized)>=3 ORDER BY length(a.normalized) DESC').fetchall()
    by_alias={}
    for row in aliases: by_alias.setdefault(row['normalized'],[]).append(row)
    for alias,rows in by_alias.items():
        # A stray source anchor must not make a different canonical person match this name.
        canonical=[r for r in rows if normalize(r['name'])==alias]
        if canonical:by_alias[alias]=canonical
    spans=[];groups=[];consumed=set();question_tokens=set(clean.split())
    for alias,rows in by_alias.items():
        if alias in STOP or alias in ROLE_WORDS:continue
        if alias.split(' ',1)[0] not in question_tokens:continue
        for match in re.finditer(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',clean):
            if any(match.start()<end and match.end()>start for start,end in spans):continue
            spans.append(match.span());consumed.update(alias.split())
            groups.append({'name':rows[0]['name'],'ids':list(dict.fromkeys(r['person_id'] for r in rows)),
                           'alias':alias,'start':match.start(),'end':match.end(),'role':None})
    # Only unambiguous near-exact single-token spelling corrections become constraints.
    for match in re.finditer(r'\w+',clean):
        token=match[0]
        if token in STOP or token in consumed or token in ROLE_WORDS or len(token)<5 or token.isdigit():continue
        candidates=sorted(((SequenceMatcher(None,token,a).ratio(),a) for a in by_alias if ' ' not in a and a not in STOP and abs(len(a)-len(token))<=1 and (a[0]==token[0] or a[-2:]==token[-2:])),reverse=True)
        if candidates and candidates[0][0]>=.84 and (len(candidates)<2 or candidates[0][0]-candidates[1][0]>=.07):
            alias=candidates[0][1];rows=by_alias[alias]
            groups.append({'name':rows[0]['name'],'ids':list(dict.fromkeys(r['person_id'] for r in rows)),
                           'alias':token,'start':match.start(),'end':match.end(),'role':None,'corrected_from':token})
            consumed.add(token)
    groups.sort(key=lambda g:g['start'])
    roles=[]
    role_pattern=r'(?<!\w)('+ '|'.join(sorted(map(re.escape,ROLE_WORDS),key=len,reverse=True))+r')(?!\w)'
    for m in re.finditer(role_pattern,clean):roles.append((m.start(),m.end(),ROLE_WORDS[m[0]]))
    for start,end,role in roles:
        if not groups:continue
        def distance(group):
            between=clean[min(end,group['end']):max(start,group['start'])]
            penalty=100 if re.search(r'\b(?:and|with|or)\b',between) else 0
            return min(abs(start-group['end']),abs(end-group['start']))+penalty
        target=min(groups,key=distance)
        if distance(target)<55:target['role']=role
    # "X and Y acted" applies actor to both; director/writer requests remain per-person.
    if groups and roles and all(r[2]=='actor' for r in roles):
        for group in groups:group['role']='actor'
    terms=[t for t in clean.split() if t not in STOP and t not in ROLE_WORDS and t not in consumed and not t.isdigit() and len(t)>2]
    # Direct movie titles should not be misread as names or generic story clues.
    title_match=con.execute('SELECT id FROM films WHERE lower(title)=lower(?) LIMIT 1',(question.strip(),)).fetchone()
    if title_match:years=[]
    year=years[0] if len(set(years))==1 else None
    year_from=min(years) if len(set(years))>1 else None
    year_to=max(years) if len(set(years))>1 else None
    if year and re.search(r'\b(?:after|since|from)\s+'+str(year),clean):
        year_from=year+(1 if 'after' in clean else 0);year=None
    if year and re.search(r'\b(?:before|until|through)\s+'+str(year),clean):
        year_to=year-(1 if 'before' in clean else 0);year=None
    return {'year':year,'year_from':year_from,'year_to':year_to,
            'person_ids':[g['ids'][0] for g in groups], 'people':groups,
            'role':roles[0][2] if len(groups)==1 and roles else None,
            'story_terms':terms[:20], 'intent':'count' if re.search(r'\bhow many|\bcount\b|\btimes\b',clean) else 'search',
            'title_id':title_match[0] if title_match else None}


def film_detail(con, fid):
    row=con.execute('SELECT * FROM films WHERE id=?',(fid,)).fetchone()
    if not row:return None
    film=dict(row)
    film['credits']=[dict(r) for r in con.execute('SELECT p.id person_id,p.name,c.role,c.character_name,c.source_url FROM credits c JOIN people p ON p.id=c.person_id WHERE c.film_id=? ORDER BY c.role,p.name',(fid,))]
    film['documents']=[dict(r) for r in con.execute('SELECT kind,text,source_url,license FROM documents WHERE film_id=? ORDER BY kind',(fid,))]
    film['facts']=[dict(r) for r in con.execute('SELECT kind,value,source_url,evidence FROM facts WHERE film_id=? ORDER BY kind,value',(fid,))]
    film['songs']=[]
    for r in con.execute('SELECT * FROM songs WHERE film_id=? ORDER BY title',(fid,)):
        song=dict(r)
        song['credits']=[dict(x) for x in con.execute('SELECT p.name,c.role,c.source_url FROM song_credits c JOIN people p ON p.id=c.person_id WHERE c.song_id=? ORDER BY c.role,p.name',(song['id'],))]
        film['songs'].append(song)
    film['links']=external_links(film)
    return film


def external_links(film):
    links=[]
    if film.get('wikipedia_url'):links.append({'label':'Wikipedia','url':film['wikipedia_url'],'verified':True})
    if film.get('imdb_id'):links.append({'label':'IMDb','url':'https://www.imdb.com/title/'+film['imdb_id']+'/','verified':True})
    for fact in film.get('facts',[]):
        if fact['kind'] in {'youtube_url','ott_url','official_trailer'}:
            links.append({'label':fact['kind'].replace('_',' ').title(),'url':fact['value'],'verified':True,'source_url':fact['source_url']})
    query=film['title']+' '+str(film.get('year') or '')+' Malayalam'
    links += [{'label':'Find trailer on YouTube','url':'https://www.youtube.com/results?search_query='+quote(query+' official trailer'),'verified':False},
              {'label':'Find streaming options (India)','url':'https://www.justwatch.com/in/search?q='+quote(film['title']),'verified':False}]
    return links


def search(con: sqlite3.Connection, question: str, limit: int = 20, use_vectors: bool = False,
           offset: int = 0, filters: dict | None = None, include_graph: bool = True, query_plan: dict | None = None) -> dict:
    filters=filters or {}
    from .plans import validate_plan,describe_plan
    parsed=validate_plan(con,query_plan,question) if query_plan is not None else parse_question(con,question)
    where=["f.language='Malayalam'"];params=[]
    def condition(sql,*values):where.append(sql);params.extend(values)
    year=filters.get('year') or parsed['year']
    if year:condition('f.year=?',int(year))
    for key,op in [('year_from','>='),('year_to','<=')]:
        val=filters.get(key) or parsed[key]
        if val:condition('f.year'+op+'?',int(val))
    if parsed['title_id']:condition('f.id=?',parsed['title_id'])
    for group in parsed['people']:
        ids=group['ids'];role=group['role'];role_sql='';values=list(ids)
        if role:
            roles=ROLE_GROUPS.get(role,[role]);role_sql=' AND c.role IN ('+','.join('?' for _ in roles)+')';values+=roles
        condition('EXISTS(SELECT 1 FROM credits c WHERE c.film_id=f.id AND c.person_id IN ('+','.join('?' for _ in ids)+')'+role_sql+')',*values)
    if filters.get('role'):condition('EXISTS(SELECT 1 FROM credits c WHERE c.film_id=f.id AND c.role=?)',filters['role'])
    for key in ('technology','location'):
        if filters.get(key):
            kinds=['technology_mention','film_format','color','aspect_ratio'] if key=='technology' else ['filming_location']
            condition('EXISTS(SELECT 1 FROM facts x WHERE x.film_id=f.id AND x.kind IN ('+','.join('?' for _ in kinds)+') AND x.value LIKE ?)',*kinds,'%'+filters[key]+'%')
    rows=con.execute('SELECT f.* FROM films f WHERE '+' AND '.join(where)+' ORDER BY f.year DESC,f.title',params).fetchall()
    terms=parsed['story_terms'];scores={}
    if terms and not parsed['title_id']:
        expression=' OR '.join('"'+t.replace('"','')+'"' for t in terms)
        # bm25 must be evaluated per matching row, outside an aggregate context.
        for r in con.execute('SELECT d.film_id,bm25(documents_fts) rank FROM documents_fts JOIN documents d ON d.id=documents_fts.rowid WHERE documents_fts MATCH ?',(expression,)):
            scores[r['film_id']]=max(scores.get(r['film_id'],0),-r['rank'])
        for r in rows:
            title=normalize(r['title'])
            hits=sum(t in title.split() for t in terms)
            if hits:scores[r['id']]=scores.get(r['id'],0)+hits*8
        # Song titles and structured evidence are independently searchable.
        for table,column in [('songs','title'),('facts','value')]:
            sql='SELECT DISTINCT film_id FROM '+table+' WHERE '+' OR '.join(column+' LIKE ?' for _ in terms)
            for r in con.execute(sql, ['%'+t+'%' for t in terms]):scores[r[0]]=scores.get(r[0],0)+2
        if not parsed['people'] and not year and not filters:
            rows=[r for r in rows if scores.get(r['id'],0)>0]
        rows=sorted(rows,key=lambda r:-scores.get(r['id'],0))
    if use_vectors and rows:
        from .vectors import vector_scores
        semantic=vector_scores(con,question,{r['id'] for r in rows})
        for fid,score in semantic.items():scores[fid]=scores.get(fid,0)+3*max(0,score)
        rows=sorted(rows,key=lambda r:-scores.get(r['id'],0))
    total=len(rows);limit=max(1,min(int(limit),100));offset=max(0,int(offset))
    selected=rows[offset:offset+limit]
    results=[]
    for row in selected:
        film=film_detail(con,row['id']);film['score']=round(scores.get(row['id'],0),3)
        film['story_terms_matched']=len(terms) if film['score'] else 0
        results.append(film)
    names=' and '.join(g['name']+(' ('+g['role'].replace('_',' ')+')' if g['role'] else '') for g in parsed['people'])
    answer=f'Found {total:,} distinct film'+('' if total==1 else 's')+(' involving '+names if names else '')
    if year:answer+=' in '+str(year)
    answer+=' in the imported catalogue.'
    if parsed['intent']=='count':answer+=' This is a catalogue count; missing or unresolved credits can change it.'
    graph=result_graph(con,[r['id'] for r in rows[:80]],parsed['people']) if include_graph else None
    return {'question':question,'parsed':parsed,'total':total,'offset':offset,'limit':limit,
            'answer':answer,'results':results,'graph':graph,
            'query_plan':describe_plan(parsed),'parser':'browser_templates' if query_plan else 'server_templates',
            'coverage_note':'Counts reflect imported source claims, not an exhaustive filmography. Same-name unlinked people remain unresolved.'}


def result_graph(con, film_ids, focus=None):
    focus=focus or [];focused={p for g in focus for p in g['ids']}
    focus_map={pid:('focus:'+str(index),group['name']) for index,group in enumerate(focus) for pid in group['ids']}
    nodes={};edges=[];seen_edges=set()
    for fid in film_ids:
        f=con.execute('SELECT title,year,wikipedia_url FROM films WHERE id=?',(fid,)).fetchone()
        if not f:continue
        nodes[fid]={'id':fid,'label':f['title'],'year':f['year'],'group':'film','url':f['wikipedia_url']}
        credits=con.execute('SELECT DISTINCT p.id,p.name,c.role FROM credits c JOIN people p ON p.id=c.person_id WHERE c.film_id=? ORDER BY c.role,p.name',(fid,)).fetchall()
        chosen=[c for c in credits if c['id'] in focused]
        chosen += [c for c in credits if c['id'] not in focused and c['role'] in {'director','writer','screenwriter','composer'}][:5]
        chosen += [c for c in credits if c['id'] not in focused and c['role']=='actor'][:5]
        chosen += [c for c in credits if c['role'] in {'singer','lyricist','choreographer','cinematographer','assistant_director'}][:4]
        for c in chosen:
            pid,name=focus_map.get(c['id'],(c['id'],c['name']))
            group='focus' if c['id'] in focused else c['role']
            existing=nodes.get(pid,{})
            roles=list(dict.fromkeys([*existing.get('roles',[]),c['role']]))
            primary=existing.get('group',group)
            nodes[pid]={'id':pid,'label':name,'group':primary,'roles':roles}
            key=(pid,fid,c['role'])
            if key in seen_edges:continue
            seen_edges.add(key);edges.append({'from':pid,'to':fid,'label':c['role'].replace('_',' '),'role':c['role']})
        for fact in con.execute("SELECT kind,value FROM facts WHERE film_id=? AND kind IN ('filming_location','technology_mention','film_format') LIMIT 6",(fid,)):
            nid=fact['kind']+':'+fact['value'];nodes[nid]={'id':nid,'label':fact['value'],'group':'location' if fact['kind']=='filming_location' else 'technology'}
            key=(fid,nid,fact['kind'])
            if key not in seen_edges:
                seen_edges.add(key);edges.append({'from':fid,'to':nid,'label':fact['kind'].replace('_',' '),'role':fact['kind']})
    return {'nodes':list(nodes.values()),'edges':edges,'film_count':len(film_ids),'limit':80,
            'note':'Graph shows up to 80 films with a selection of credits per film. Counts use all matching films.'}


def person_graph(con,name):
    result=search(con,name,limit=20)
    if not result['parsed']['people']:return {'error':f"Person '{name}' not found in the database."}
    graph=result['graph'];graph['person']=result['parsed']['people'][0]['name'];graph['total']=result['total']
    return graph


def coverage(con):
    scalar=lambda sql:con.execute(sql).fetchone()[0]
    return {'films':scalar('SELECT count(*) FROM films'),'people':scalar('SELECT count(*) FROM people'),
            'credits':scalar('SELECT count(*) FROM credits'),'songs':scalar('SELECT count(*) FROM songs'),
            'documents':scalar('SELECT count(*) FROM documents'),'imdb_links':scalar('SELECT count(*) FROM films WHERE imdb_id IS NOT NULL'),
            'plots':scalar("SELECT count(DISTINCT film_id) FROM documents WHERE kind='plot'"),
            'locations':scalar("SELECT count(DISTINCT film_id) FROM facts WHERE kind='filming_location'"),
            'technical':scalar("SELECT count(DISTINCT film_id) FROM facts WHERE kind IN ('technology_mention','film_format','color','aspect_ratio')"),
            'years':[dict(r) for r in con.execute('SELECT year,count(*) films FROM films GROUP BY year ORDER BY year')],
            'roles':[dict(r) for r in con.execute('SELECT role,count(*) credits,count(DISTINCT film_id) films FROM credits GROUP BY role ORDER BY credits DESC')],
            'jobs':[dict(r) for r in con.execute('SELECT source,status,count(*) count,max(updated_at) updated_at FROM import_jobs GROUP BY source,status')],
            'technologies':[r[0] for r in con.execute("SELECT DISTINCT value FROM facts WHERE kind IN ('technology_mention','film_format') ORDER BY value")],
            'top_locations':[dict(r) for r in con.execute("SELECT value,count(DISTINCT film_id) films FROM facts WHERE kind='filming_location' GROUP BY value ORDER BY films DESC LIMIT 40")],
            'source_status':{'wikipedia':'Public lists and film pages; incremental import', 'wikidata':'Public structured credits and identifiers',
                             'imdb':'Noncommercial datasets; only exact linked title IDs', 'letterboxd':'No catalogue feed: API access is restricted'},
            'note':'Coverage is incomplete. Unlinked names are source-scoped; ambiguous titles are not guessed. Technical mentions carry evidence and may refer to re-releases.'}
