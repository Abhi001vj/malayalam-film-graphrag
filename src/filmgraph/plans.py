"""Whitelist query plans: the browser supplies entities, never SQL or Cypher."""
from __future__ import annotations
from .db import normalize

INTENTS={'collaboration_count','shared_films','person_filmography','story_search','film_lookup'}
ROLES={'actor','director','writer','screenwriter','story_writer','producer','composer','score_composer','singer','lyricist','cinematographer','choreographer','assistant_director','second_unit_director','editor','production_designer','costume_designer','makeup_artist','art_director','sound_designer','executive_producer'}


def entity_dictionary(con):
    return [dict(r) for r in con.execute("SELECT a.normalized alias,p.name name,count(DISTINCT c.film_id) films FROM aliases a JOIN people p ON p.id=a.person_id JOIN credits c ON c.person_id=p.id WHERE p.id NOT LIKE 'unresolved:%' GROUP BY a.normalized,p.name HAVING length(a.normalized)>=3 ORDER BY length(a.normalized) DESC,films DESC")]


def validate_plan(con,plan,question):
    if not isinstance(plan,dict) or plan.get('version')!=1 or plan.get('intent') not in INTENTS:
        raise ValueError('Unsupported query plan')
    people=plan.get('people',[])
    if not isinstance(people,list) or len(people)>6:raise ValueError('At most six people per query')
    groups=[]
    for person in people:
        name=person.get('name','')
        if not isinstance(name,str) or not 1<=len(name)<=150:raise ValueError('Invalid person name')
        role=person.get('role')
        if role is not None and role not in ROLES:raise ValueError('Unsupported role')
        alias=normalize(name)
        rows=con.execute('SELECT a.person_id,p.name FROM aliases a JOIN people p ON p.id=a.person_id WHERE a.normalized=?',(alias,)).fetchall()
        canonical=[r for r in rows if normalize(r['name'])==alias]
        ids=[r[0] for r in canonical or rows]
        if not ids:raise ValueError('Unknown person in query plan: '+name)
        groups.append({'name':name,'alias':alias,'ids':ids,'role':role})
    year=plan.get('year');year_from=plan.get('year_from');year_to=plan.get('year_to')
    for value in (year,year_from,year_to):
        if value is not None and (type(value) is not int or not 1900<=value<=2100):raise ValueError('Invalid year')
    terms=plan.get('terms',[])
    if not isinstance(terms,list) or len(terms)>20 or any(not isinstance(t,str) or len(t)>80 for t in terms):raise ValueError('Invalid story terms')
    title=con.execute('SELECT id FROM films WHERE lower(title)=lower(?) LIMIT 1',(question.strip(),)).fetchone() if plan['intent']=='film_lookup' else None
    return {'year':year,'year_from':year_from,'year_to':year_to,'people':groups,
            'person_ids':[g['ids'][0] for g in groups],'role':groups[0]['role'] if len(groups)==1 else None,
            'story_terms':[normalize(t) for t in terms if normalize(t)],'intent':'count' if plan['intent']=='collaboration_count' else 'search','title_id':title[0] if title else None}


def describe_plan(parsed):
    people=parsed['people']
    intent='collaboration_count' if parsed['intent']=='count' else 'shared_films' if len(people)>1 else 'person_filmography' if people else 'film_lookup' if parsed.get('title_id') else 'story_search'
    return {'version':1,'intent':intent,'people':[{'name':g['name'],'role':g.get('role')} for g in people],
            'year':parsed['year'],'year_from':parsed.get('year_from'),'year_to':parsed.get('year_to'),'terms':parsed['story_terms']}
