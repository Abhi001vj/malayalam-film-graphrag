"""Resumable Wikipedia catalogue, film pages, soundtrack and technical evidence."""
from __future__ import annotations
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .db import connect, import_records, normalize
from .http import Client

WIKI = 'https://en.wikipedia.org'
LICENSE = 'CC BY-SA 4.0; attribution and revision at source URL'
ROLE_HEADERS = {
    'direction': 'director', 'main actors': 'actor', 'cast s': 'actor',
    'story': 'story_writer', 'screenplay': 'screenwriter',
    'director': 'director', 'directed by': 'director', 'directors': 'director',
    'cast': 'actor', 'starring': 'actor', 'written by': 'writer', 'writer': 'writer',
    'screenplay by': 'screenwriter', 'story by': 'story_writer',
    'produced by': 'producer', 'producer': 'producer',
    'music': 'composer', 'music by': 'composer', 'music director': 'composer',
    'cinematography': 'cinematographer', 'edited by': 'editor', 'editor': 'editor',
    'lyrics': 'lyricist', 'lyricist': 'lyricist', 'lyricists': 'lyricist',
    'singer s': 'singer', 'singers': 'singer', 'singer': 'singer', 'vocals': 'singer',
    'choreography': 'choreographer', 'choreographer': 'choreographer',
    'assistant director': 'assistant_director', 'assistant directors': 'assistant_director',
    'art direction': 'art_director', 'production design': 'production_designer',
    'costume design': 'costume_designer', 'sound design': 'sound_designer',
    'visual effects': 'vfx_artist', 'vfx': 'vfx_artist',
    'action choreography': 'stunt_coordinator', 'stunts': 'stunt_coordinator',
}
TECH = [
    ('CinemaScope', r'\b(?:cinemascope|cinema scope)\b'),
    ('70 mm film', r'\b70\s*mm\b'), ('35 mm film', r'\b35\s*mm\b'),
    ('16 mm film', r'\b16\s*mm\b'), ('Digital cinematography', r'\b(?:shot|filmed|shooting|cinematography)\b[^.!?]{0,80}\bdigital\b|\bdigital\s+(?:camera|cinematography|filmmaking)\b'),
    ('3D', r'\b(?:3-d|3d|stereoscopic)\b'),
    ('VFX', r'\b(?:vfx|visual effects|computer.generated imagery|cgi)\b'),
    ('Black and white', r'\bblack.and.white\b'),
    ('Technicolor', r'\btechnicolor\b'), ('Eastmancolor', r'\beastman\s*colou?r\b'),
    ('Dolby Atmos', r'\bdolby atmos\b'), ('Motion capture', r'\bmotion capture\b'),
    ('IMAX', r'\bimax\b'), ('ARRI Alexa', r'\b(?:arri\s+)?alexa\b'),
    ('RED camera', r'\bred\s+(?:epic|dragon|one|camera|weapon|monstro)\b'),
]


def page_url(title: str) -> str:
    return WIKI + '/wiki/' + quote(title.replace(' ', '_'), safe='():,')


def wiki_identity(url: str) -> str:
    return 'wiki:' + unquote(urlparse(url).path.split('/wiki/', 1)[-1]).replace(' ', '_')


def clean_text(node) -> str:
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def article_link(a) -> str | None:
    href = a.get('href', '')
    url = urljoin(WIKI, href)
    parsed = urlparse(url)
    if parsed.netloc != 'en.wikipedia.org' or not parsed.path.startswith('/wiki/'):
        return None
    title = unquote(parsed.path[6:])
    if re.match(r'^(?:Category|File|Template|Help|Wikipedia|Portal|Special|Talk|User):', title, re.I) or title.startswith(('List_of_', 'Lists_of_')):
        return None
    return page_url(title)


def people_in(cell) -> list[dict]:
    """Resolve linked people exactly; keep unlinked names scoped and explicitly unresolved."""
    people = []
    clone = BeautifulSoup(str(cell), 'html.parser')
    for noise in clone.select('sup, .sortkey, .sorttext, .mw-editsection'):
        noise.decompose()
    for a in clone.select('a[href]'):
        url = article_link(a)
        name = clean_text(a)
        if normalize(name) in {'tamil','malayalam','hindi','english','telugu','kannada','sanskrit','traditional','chorus','vocals','lyrics','background score','songs','film score','instrumental'}:
            a.replace_with(' | ')
            continue
        if url and name and not re.fullmatch(r'[\d\W]+', name):
            people.append({'id': wiki_identity(url), 'name': name})
            a.replace_with(' | ')
    # Track lists and year tables contain many names without their own articles.
    for br in clone.select('br'):
        br.replace_with(' | ')
    for li in clone.select('li'):
        li.append(' | ')
    rest = clean_text(clone)
    for name in re.split(r'\s*[,;|\n]\s*|\s+and\s+', rest):
        name = re.sub(r'\([^)]*\)', '', name).strip(' .:')
        if not name or name.casefold() in {'and', 'none', 'n/a', 'tba', 'tbd', 'various', 'chorus', 'traditional', 'songs', 'score', 'background score', 'lyrics', '—', '–', '-'}:
            continue
        if re.search(r'\b(?:songs?|score|background|lyrics|vocals|tamil|malayalam|hindi)\s*:', name, re.I):
            continue
        if re.search(r'\b(?:based on|legend of|mythology|adapted from|debut|travancore sisters)\b', name, re.I):
            continue
        if len(name) < 2 or len(name) > 80 or not re.search('[A-Za-z\u0d00-\u0d7f]', name):
            continue
        # Unlinked identities are scoped to the source page, avoiding speculative merges.
        people.append({'id': 'unresolved:' + hashlib.sha1((getattr(cell, '_source_url', '') or '' + name).encode()).hexdigest(), 'name': name})
    return list({p['id']: p for p in people}.values())


def table_rows(table):
    """Expand row/column spans so release dates cannot shift a title into another column."""
    spans = {}
    for tr in table.find_all('tr'):
        if tr.find_parent('table') is not table:
            continue
        cells, col = [], 0
        for cell in tr.find_all(['td', 'th'], recursive=False):
            while col in spans:
                left, old = spans[col]; cells.append(old)
                if left <= 1: del spans[col]
                else: spans[col] = (left - 1, old)
                col += 1
            for _ in range(int(cell.get('colspan', 1))):
                cells.append(cell)
                if int(cell.get('rowspan', 1)) > 1:
                    spans[col] = (int(cell.get('rowspan')) - 1, cell)
                col += 1
        while col in spans:
            left, old = spans[col]; cells.append(old)
            if left <= 1: del spans[col]
            else: spans[col] = (left - 1, old)
            col += 1
        yield tr, cells


def parse_list(html: bytes | str, url: str, year: int | None = None) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    for noise in soup.select('sup.reference'):
        noise.decompose()
    records = {}
    current_year = year
    for table in soup.select('table.wikitable'):
        headers = None
        for tr, cells in table_rows(table):
            header_aliases = {'film':'title','movie':'title','movies':'title','direction':'director'}
            labels = [header_aliases.get(normalize(clean_text(c)),normalize(clean_text(c))) for c in cells]
            if 'title' in labels and any(h in labels for h in ('director', 'directors', 'cast')):
                headers = labels
                continue
            if headers is None or len(cells) != len(headers):
                continue
            values = dict(zip(headers, cells))
            if 'year' in values or 'date' in values:
                m = re.search(r'\b(?:19|20)\d{2}\b', clean_text(values.get('year',values.get('date'))))
                if m: current_year = int(m[0])
            title_cell = values.get('title')
            title = clean_text(title_cell).strip('*†‡ ')
            if not title or title.casefold() == 'title' or not current_year:
                continue
            a = next((a for a in title_cell.select('a[href]') if article_link(a)), None)
            wiki = article_link(a) if a else None
            fid = wiki_identity(wiki) if wiki else 'list:' + hashlib.sha1(f'{title}:{current_year}'.encode()).hexdigest()
            film = {'id': fid, 'title': title, 'year': current_year, 'wikipedia_url': wiki,
                    'credits': [], 'facts': [{'kind': 'catalogue_source', 'value': url, 'source_url': url}],
                    'documents': []}
            for header, cell in values.items():
                role = ROLE_HEADERS.get(header)
                if role:
                    if role=='story_writer' and re.match(r'^(?:based on|mythology|adapted from)',clean_text(cell),re.I):continue
                    for p in people_in(cell):
                        if p['id'].startswith('unresolved:'):
                            p['id'] = 'unresolved:' + hashlib.sha1((url + ':' + p['name']).encode()).hexdigest()
                        film['credits'].append({'person': p, 'role': role, 'source_url': url})
            records[fid] = film
    return list(records.values())


def section_content(soup):
    root = soup.select_one('.mw-parser-output') or soup
    current = 'lead'
    sections = {}
    # Heading wrappers vary between desktop and Parsoid HTML.
    for node in root.find_all(['h2', 'h3', 'p', 'ul', 'ol', 'table']):
        if node.find_parent(['table', 'ul', 'ol']):
            continue
        if node.name in {'h2', 'h3'}:
            current = re.sub(r'\[\s*edit\s*\]', '', clean_text(node), flags=re.I).strip().casefold()
        else:
            sections.setdefault(current, []).append(node)
    return sections


def prose_credits(node, source):
    """Recognize only explicit linked-name credit clauses, never occupations from a plot."""
    clone=BeautifulSoup(str(node),'html.parser')
    people={}
    role_terms={'assistant director','assistant directors','second unit director','second unit directors',
                'choreographer','choreographers','choreography','costume designer','costume design',
                'makeup artist','make up artist','art director','art direction','production designer','sound designer'}
    for a in list(clone.select('a[href]')):
        url=article_link(a);name=clean_text(a)
        if not url or normalize(name) in role_terms:continue
        marker='__P'+str(len(people))+'__'
        people[marker]={'id':wiki_identity(url),'name':name}
        a.replace_with(marker)
    text=clean_text(clone)
    names=r'(?P<names>__P\d+__(?:\s*(?:,|and|&|/)\s*__P\d+__)*)'
    clauses=[
        ('assistant_director',names+r'\s+(?:served|worked|acted)\s+as\s+(?:the\s+)?assistant directors?'),
        ('second_unit_director',names+r'\s+(?:served|worked|acted)\s+as\s+(?:the\s+)?second[- ]unit directors?'),
        ('choreographer',r'choreograph(?:y|ed)(?:\s+was|\s+were)?(?:\s+handled|\s+done)?\s+by\s+'+names),
        ('costume_designer',r'costumes?(?:\s+design)?(?:\s+was|\s+were)?(?:\s+designed|\s+created|\s+handled)?\s+by\s+'+names),
        ('makeup_artist',r'make[- ]?up(?:\s+was|\s+were)?(?:\s+handled|\s+done)?\s+by\s+'+names),
        ('art_director',r'art direction(?:\s+was|\s+were)?(?:\s+handled|\s+done)?\s+by\s+'+names),
        ('production_designer',r'production design(?:\s+was|\s+were)?(?:\s+handled|\s+done)?\s+by\s+'+names),
        ('sound_designer',r'sound design(?:\s+was|\s+were)?(?:\s+handled|\s+done)?\s+by\s+'+names),
    ]
    result=[]
    for role,pattern in clauses:
        for match in re.finditer(pattern,text,re.I):
            for marker in re.findall(r'__P\d+__',match.group('names')):
                result.append({'person':people[marker],'role':role,'source_url':source})
    return result


def parse_film(html: bytes | str, film: dict) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    url = film['wikipedia_url']
    revision = soup.select_one('#t-permalink a')
    source = urljoin(WIKI, revision['href']) if revision else url
    for noise in soup.select('sup.reference, .mw-editsection, script, style'):
        noise.decompose()
    result = {k: v for k, v in film.items() if k in {'id','title','year','wikipedia_url','imdb_id'}}
    result.update(credits=[], documents=[], facts=[], songs=[], revision_url=source)
    infobox = soup.select_one('table.infobox')
    qlink = soup.select_one('a[href*="wikidata.org/wiki/Special:EntityPage/"]')
    if qlink:
        qid = re.search(r'Q\d+', qlink['href'])
        if qid:
            result['facts'].append({'kind': 'wikidata_id', 'value': qid[0], 'source_url': source})
    for a in soup.select('a[href*="imdb.com/title/tt"]'):
        m = re.search(r'/title/(tt\d+)', a['href'])
        if m: result['imdb_id'] = m[1]; break
    for a in soup.select('a[href]'):
        target=a['href'];host=urlparse(target).hostname or ''
        if host in {'www.youtube.com','youtube.com','youtu.be'} and ('/watch?' in target or host=='youtu.be'):
            result['facts'].append({'kind':'youtube_url','value':target,'source_url':source,'evidence':clean_text(a)})
        if host in {'netflix.com','www.netflix.com','www.primevideo.com','primevideo.com','www.hotstar.com','www.jiohotstar.com','www.zee5.com','www.sonyliv.com','www.sunnxt.com','www.manoramamax.com'} and len(urlparse(target).path)>6:
            result['facts'].append({'kind':'ott_url','value':target,'source_url':source,'evidence':clean_text(a)})
        if host=='letterboxd.com' and '/film/' in target:
            result['facts'].append({'kind':'letterboxd_url','value':target,'source_url':source,'evidence':clean_text(a)})
    if infobox:
        for tr in infobox.select('tr'):
            if tr.find_parent('table') is not infobox: continue
            th, td = tr.find('th'), tr.find('td')
            if not th or not td: continue
            label = normalize(clean_text(th))
            role = ROLE_HEADERS.get(label)
            if role:
                segments = re.split(r'<br\s*/?>', td.decode_contents(), flags=re.I) if role=='composer' else [td.decode_contents()]
                actual_role=role
                for segment in segments:
                    cell=BeautifulSoup('<td>'+segment+'</td>','html.parser').find('td')
                    label_text=clean_text(cell).casefold()
                    if role=='composer':
                        if label_text.startswith('lyrics:'): actual_role='lyricist'
                        elif label_text.startswith(('background score:', 'score:')): actual_role='score_composer'
                        elif label_text.startswith(('songs:', 'music:')): actual_role='composer'
                    for person in people_in(cell):
                        if person['id'].startswith('unresolved:'):
                            person['id'] = 'unresolved:' + hashlib.sha1((url + ':' + person['name']).encode()).hexdigest()
                        result['credits'].append({'person': person, 'role': actual_role, 'source_url': source})
            if label in {'running time', 'release date', 'release dates', 'language', 'budget', 'box office', 'production company', 'production companies'}:
                result['facts'].append({'kind': label.replace(' ', '_'), 'value': clean_text(td), 'source_url': source})
    sections = section_content(soup)
    for heading, nodes in sections.items():
        if heading in {'references', 'external links', 'see also', 'contents', 'notes', 'awards', 'remakes'}:
            continue
        paragraphs = [clean_text(n) for n in nodes if n.name == 'p']
        text = '\n'.join(p for p in paragraphs if p)
        if text and (heading == 'lead' or re.search(r'plot|story|synopsis|production|filming|development|soundtrack|music|technical|release|dubbing', heading)):
            kind = 'plot' if heading in {'plot','story','synopsis'} else heading
            result['documents'].append({'kind': kind, 'text': text[:40000], 'source_url': source, 'license': LICENSE})
        if re.search('release|distribution|home media',heading):
            for paragraph in paragraphs:
                if re.search(r'\b(?:streaming|digital rights|ott|netflix|prime video|hotstar|zee5|sonyliv|sunnxt|manorama)\b',paragraph,re.I):
                    result['facts'].append({'kind':'streaming_evidence','value':paragraph,'source_url':source})
        if heading == 'cast':
            for node in nodes:
                for li in node.find_all('li',recursive=False) if node.name in {'ul','ol'} else []:
                    a = li.find('a', href=True)
                    if not a or not article_link(a): continue
                    person = {'id': wiki_identity(article_link(a)), 'name': clean_text(a)}
                    character = re.split(r'\bas\b', clean_text(li), maxsplit=1)
                    result['credits'].append({'person': person, 'role': 'actor', 'character': character[1].strip() if len(character)>1 else '', 'source_url': source})
        if re.search(r'production|filming|technical|development|lead', heading):
            for node in nodes:
                if node.name=='p':result['credits'].extend(prose_credits(node,source))
            for sentence in re.split(r'(?<=[.!?])\s+', text):
                if re.search(r'\b(?:not|never|without)\b', sentence, re.I):
                    continue
                for technology, pattern in TECH:
                    if re.search(pattern, sentence, re.I):
                        result['facts'].append({'kind': 'technology_mention', 'value': technology, 'source_url': source, 'evidence': sentence})
            for node in nodes:
                if node.name != 'p': continue
                paragraph = clean_text(node)
                if not re.search(r'\b(?:shot|filmed|filming|shooting|locations?)\b', paragraph, re.I): continue
                # Preserve context as evidence; linked places are verified by Wikidata in the structured import.
                result['facts'].append({'kind':'filming_evidence','value':paragraph[:4000],'source_url':source,'evidence':paragraph[:4000]})
        if re.search(r'soundtrack|music|songs', heading):
            for table in [n for n in nodes if n.name == 'table']:
                song_headers = None
                for tr, cells in table_rows(table):
                    labels = [normalize(clean_text(c)) for c in cells]
                    if any(h in labels for h in ('title','song','song title')) and any('singer' in h or h in {'lyrics','lyricist','vocals'} for h in labels):
                        song_headers = labels; continue
                    if not song_headers or len(cells) != len(song_headers): continue
                    values = dict(zip(song_headers,cells))
                    tc = next((values[h] for h in ('title','song','song title') if h in values), None)
                    title = clean_text(tc).strip(' "“”')
                    if not title or title.casefold() in {'title','song'}: continue
                    song = {'title': title, 'source_url': source, 'credits': []}
                    for header, cell in values.items():
                        role = 'singer' if 'singer' in header or header == 'vocals' else ROLE_HEADERS.get(header)
                        if role not in {'singer','lyricist','composer'}: continue
                        for p in people_in(cell):
                            if p['id'].startswith('unresolved:'):
                                p['id']='unresolved:'+hashlib.sha1((url+':'+p['name']).encode()).hexdigest()
                            credit = {'person':p,'role':role,'source_url':source}
                            song['credits'].append(credit)
                            result['credits'].append(credit)
                    # A soundtrack composer is attached to songs only when the article names one composer.
                    composers = {c['person']['id']: c for c in result['credits'] if c['role']=='composer'}
                    if len(composers)==1:
                        song['credits'].extend(composers.values())
                    result['songs'].append(song)
    return result


def mark_job(con, source, item, status, error=''):
    con.execute('INSERT INTO import_jobs(source,item,status,error) VALUES(?,?,?,?) '
                'ON CONFLICT(source,item) DO UPDATE SET status=excluded.status,error=excluded.error,updated_at=CURRENT_TIMESTAMP',
                (source,item,status,error[:2000]))
    con.commit()


def import_catalogue(con, client: Client, start: int = 1928, end: int | None = None, refresh: bool = False) -> int:
    end = end or date.today().year
    titles = [('List_of_Malayalam_films_before_1960',None)] if start < 1960 else []
    titles += [(f'List_of_Malayalam_films_of_{y}',y) for y in range(max(1960,start),end+1)]
    total=0
    for title,year in titles:
        url=page_url(title)
        done=con.execute("SELECT status FROM import_jobs WHERE source='wikipedia_list' AND item=?",(url,)).fetchone()
        if done and done[0]=='complete' and not refresh: continue
        try:
            html=client.get(url,refresh=refresh)
            records=[r for r in parse_list(html,url,year) if start <= r['year'] <= end]
            if not records: raise ValueError('No film rows parsed; review page layout')
            count=import_records(con,records);total+=count
            soup=BeautifulSoup(html,'html.parser'); revision=soup.select_one('#t-permalink a')
            con.execute('INSERT OR REPLACE INTO source_pages(url,revision_url,license) VALUES(?,?,?)',
                        (url,urljoin(WIKI,revision['href']) if revision else url,LICENSE))
            mark_job(con,'wikipedia_list',url,'complete')
            print(f'Catalogue {title}: {count} films',flush=True)
        except Exception as exc:
            mark_job(con,'wikipedia_list',url,'failed',str(exc))
            print(f'Catalogue failed {title}: {exc}',flush=True)
    return total


def enrich_pages(db_path, client: Client, limit: int | None = None, workers: int = 3, refresh: bool = False) -> int:
    with connect(db_path) as con:
        sql="SELECT f.* FROM films f WHERE wikipedia_url IS NOT NULL"
        if not refresh: sql+=" AND NOT EXISTS(SELECT 1 FROM import_jobs j WHERE j.source='wikipedia_page' AND j.item=f.wikipedia_url AND j.status='complete')"
        sql+=' ORDER BY f.year DESC,f.title'
        if limit: sql+=f' LIMIT {int(limit)}'
        films=[dict(r) for r in con.execute(sql)]
        print(f'Enriching {len(films)} Wikipedia film pages',flush=True)
        count=0
        def fetch(film):
            return parse_film(client.get(film['wikipedia_url'],refresh=refresh),film)
        with ThreadPoolExecutor(max_workers=max(1,min(workers,4))) as pool:
            # Small waves bound memory, let SQLite commit incrementally, and resume safely.
            for start in range(0,len(films),40):
                futures={pool.submit(fetch,f):f for f in films[start:start+40]}
                for future in as_completed(futures):
                    film=futures[future];url=film['wikipedia_url']
                    try:
                        record=future.result()
                        import_records(con,[record])
                        con.execute('INSERT OR REPLACE INTO source_pages(url,revision_url,license) VALUES(?,?,?)',(url,record['revision_url'],LICENSE))
                        mark_job(con,'wikipedia_page',url,'complete');count+=1
                    except Exception as exc:
                        mark_job(con,'wikipedia_page',url,'failed',str(exc))
                        print(f'Page failed {film["title"]}: {exc}',flush=True)
                print(f'Wikipedia pages {min(start+40,len(films))}/{len(films)}; imported {count}',flush=True)
    return count
