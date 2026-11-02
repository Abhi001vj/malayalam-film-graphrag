"""How each actor appears in each film: a real role, a cameo, as themselves, voice only, or archive footage.

A credit row only says "actor". The qualifier lives in the source text: "Mammootty as the narrator (voice only)",
"Mohanlal (television scene)", "Mammootty as himself (cameo appearance)", or an infobox "Narrated by" field.
`build_appearances` reads the stored character text for every source plus the cached Wikipedia page
(cast list items, the sub-heading above them and the infobox) and writes one derived row per actor credit
to kg_appearances. Raw tables are never modified. No network calls: pages come from the HTTP cache.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import time
from pathlib import Path

from bs4 import BeautifulSoup

from .catalogue import article_link, clean_text, section_content, wiki_identity

KINDS = ['role', 'cameo', 'self', 'voice', 'archive']
LABELS = {'role': 'acted', 'cameo': 'cameo', 'self': 'as self', 'voice': 'voice / narration',
          'archive': 'archive footage', 'unconfirmed': 'unconfirmed'}
# Checked in this order; the first match wins for one piece of text.
PATTERNS = [
    ('voice', r'\bvoice(?:[- ]?over|d| only| role| cameo)?\b|\bnarrat(?:or|ion|ed|es)\b|\bdubb(?:ed|ing)\b'),
    ('archive', r'\barchiv(?:e|al)\b|\bfootage\b|\btelevision scene\b|\btv scene\b|\bphoto(?:graph)?\b|\bposter\b|\bstill image\b'),
    ('self', r'\b(?:him|her|them)sel(?:f|ves)\b'),
    ('cameo', r'\bcameo\b|\bguest (?:appearance|role)\b|\bspecial appearance\b|\bfriendly appearance\b|\bextended cameo\b'),
]
# "Nandini (Voice by Bhagyalekshmi)" credits the dubbing artist; the actor is still on screen.
DUBBED_BY = r'\b(?:voice[sd]?\s+(?:dubbed\s+)?|dubbed\s+|dubbing\s+)by\b[^)]*'
CACHE = Path('data/cache/http')

SCHEMA = """CREATE TABLE kg_appearances(film_id TEXT NOT NULL, person_id TEXT NOT NULL, kind TEXT NOT NULL,
            evidence TEXT, source_url TEXT, PRIMARY KEY(film_id, person_id))"""


def classify(text: str) -> str | None:
    """Kind signalled by one piece of credit text, or None when it says nothing beyond a character name."""
    t = re.sub(DUBBED_BY, ' ', (text or '').casefold())
    for kind, pattern in PATTERNS:
        if re.search(pattern, t):
            return kind
    return None


def decide(signals: list[tuple[str, str, str, bool]]) -> tuple[str, str, str]:
    """signals: (kind, evidence, source_url, strong). One rule, in order:
    1. a named character with no qualifier, or an infobox Starring entry, means a real role;
    2. otherwise the clearest qualifier wins: voice/archive (not on screen), then as-themselves, then cameo;
    3. otherwise (a bare listing, or a Wikidata/IMDb claim with no text) it counts as a role."""
    strong = [s for s in signals if s[0] == 'role' and s[3]]
    if strong:
        return strong[0][:3]
    for kind in ('voice', 'archive', 'self', 'cameo'):
        hit = next((s for s in signals if s[0] == kind), None)
        if hit:
            return hit[:3]
    return signals[0][:3] if signals else ('role', '', '')


def _page(url: str):
    path = CACHE / (hashlib.sha256(url.encode()).hexdigest() + '.gz')
    if not path.exists():
        return None
    return BeautifulSoup(gzip.decompress(path.read_bytes()).decode('utf-8', 'replace'), 'html.parser')


def page_signals(soup, url: str) -> dict[str, list]:
    """raw person id -> signals from the cast section and the infobox of one film page."""
    out: dict[str, list] = {}
    add = lambda pid, *s: out.setdefault(pid, []).append(s)
    box = soup.select_one('.infobox')
    if box:
        for th in box.select('th'):
            label = clean_text(th).casefold()
            td = th.find_next_sibling('td')
            if not td or label not in ('starring', 'narrated by', 'voices of', 'voice of'):
                continue
            for a in td.find_all('a', href=True):
                if article_link(a):
                    pid = wiki_identity(article_link(a))
                    if label == 'starring':
                        add(pid, 'role', 'Infobox: Starring', url, True)
                    else:
                        add(pid, 'voice', f'Infobox: {clean_text(th)}', url, False)
    for heading, nodes in section_content(soup).items():
        if not re.match(r'(?:main |principal |supporting )?(?:cast|voice cast|cast and characters)\b', heading):
            continue
        context = ''
        for node in nodes:
            if node.name == 'p':  # a sub-heading such as "Narrators" or "Cameo appearances"
                context = clean_text(node)
                continue
            for li in node.find_all('li') if node.name in ('ul', 'ol') else []:
                a = li.find('a', href=True)
                if not a or not article_link(a):
                    continue
                alone = BeautifulSoup(str(li), 'html.parser')
                for nested in alone.find_all(['ul', 'ol']):
                    nested.decompose()  # "Mammootty as Veeru" must not pick up "Anu Mohan as Young Veeru"
                own = clean_text(alone)
                parent = li.find_parent('li')
                group = context + ' ' + (clean_text(parent.find(string=True, recursive=False) or '') if parent else '')
                prev = node.find_previous_sibling(['dl', 'h4', 'p'])
                if prev is not None and prev.name in ('dl', 'h4'):
                    group += ' ' + clean_text(prev)
                kind = classify(own) or classify(group)
                strong = bool(re.search(r'\bas\b', own)) and kind is None
                add(wiki_identity(article_link(a)), kind or 'role', own[:300], url, strong)
    return out


def build_appearances(con: sqlite3.Connection, limit: int | None = None, log=print) -> dict:
    started = time.perf_counter()
    con.execute('DROP TABLE IF EXISTS kg_appearances')
    con.execute(SCHEMA)
    person_map = dict(con.execute('SELECT person_id, canonical_id FROM kg_person_map'))
    film_map = dict(con.execute('SELECT film_id, canonical_id FROM kg_film_map'))
    films = con.execute("""SELECT DISTINCT c.film_id, f.wikipedia_url FROM kg_credits c JOIN kg_films f ON f.id=c.film_id
                           WHERE c.role='actor' ORDER BY c.film_id""").fetchall()[:limit]
    canonical_to_raw: dict[str, list[str]] = {}
    for raw, canon in film_map.items():
        canonical_to_raw.setdefault(canon, []).append(raw)
    counts = {k: 0 for k in KINDS}
    for n, (fid, wiki_url) in enumerate(films, 1):
        signals: dict[str, list] = {}
        raws = canonical_to_raw.get(fid, [fid])
        marks = ','.join('?' * len(raws))
        for pid, character, source in con.execute(
                f"SELECT person_id, character_name, source_url FROM credit_sources WHERE role='actor' AND film_id IN ({marks})", raws):
            canon = person_map.get(pid, pid)
            kind = classify(character)
            strong = bool(character) and kind is None and 'wikipedia.org' in (source or '')
            signals.setdefault(canon, []).append((kind or 'role', character or '', source, strong))
        if wiki_url:
            soup = _page(wiki_url)
            if soup is not None:
                for pid, items in page_signals(soup, wiki_url).items():
                    signals.setdefault(person_map.get(pid, pid), []).extend(items)
        rows = []
        for pid, in con.execute("SELECT DISTINCT person_id FROM kg_credits WHERE film_id=? AND role='actor'", (fid,)):
            kind, evidence, source = decide(signals.get(pid, []))
            counts[kind] += 1
            rows.append((fid, pid, kind, evidence, source))
        con.executemany('INSERT OR REPLACE INTO kg_appearances VALUES (?,?,?,?,?)', rows)
        if n % 500 == 0:
            con.commit()
            log(f'{n}/{len(films)} films · {counts}')
    con.commit()
    return {'films': len(films), 'appearances': counts, 'seconds': round(time.perf_counter() - started, 1)}


def lookup(con: sqlite3.Connection, film_id: str, person_id: str) -> tuple[str, str, str] | None:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_appearances'").fetchone():
        return None
    return con.execute('SELECT kind, evidence, source_url FROM kg_appearances WHERE film_id=? AND person_id=?',
                       (film_id, person_id)).fetchone()
