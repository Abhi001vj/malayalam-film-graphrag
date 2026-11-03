"""SQLite property graph, source provenance, and story text index."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import unquote


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
CREATE TABLE IF NOT EXISTS films (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER,
  language TEXT NOT NULL DEFAULT 'Malayalam', wikipedia_url TEXT, imdb_id TEXT
);
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_identifiers (
 identifier TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id)
);
CREATE TABLE IF NOT EXISTS aliases (
  person_id TEXT NOT NULL REFERENCES people(id), alias TEXT NOT NULL,
  normalized TEXT NOT NULL, PRIMARY KEY(person_id, normalized)
);
CREATE INDEX IF NOT EXISTS aliases_normalized ON aliases(normalized);
CREATE TABLE IF NOT EXISTS credits (
  film_id TEXT NOT NULL REFERENCES films(id),
  person_id TEXT NOT NULL REFERENCES people(id),
  role TEXT NOT NULL, character_name TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL, PRIMARY KEY(film_id, person_id, role, character_name)
);
CREATE INDEX IF NOT EXISTS credits_person_role ON credits(person_id, role, film_id);
CREATE INDEX IF NOT EXISTS credits_film ON credits(film_id);
CREATE INDEX IF NOT EXISTS films_imdb ON films(imdb_id);
CREATE INDEX IF NOT EXISTS films_wikipedia ON films(wikipedia_url);
CREATE TABLE IF NOT EXISTS film_identifiers (
 identifier TEXT PRIMARY KEY, film_id TEXT NOT NULL REFERENCES films(id)
);
CREATE TABLE IF NOT EXISTS credit_sources (
 film_id TEXT NOT NULL REFERENCES films(id), person_id TEXT NOT NULL REFERENCES people(id),
 role TEXT NOT NULL, character_name TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL,
 retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(film_id,person_id,role,character_name,source_url)
);
CREATE TABLE IF NOT EXISTS facts (
 film_id TEXT NOT NULL REFERENCES films(id), kind TEXT NOT NULL, value TEXT NOT NULL,
 source_url TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
 retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(film_id,kind,value,source_url)
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts(kind,value,film_id);
CREATE INDEX IF NOT EXISTS facts_film ON facts(film_id,kind);
CREATE TABLE IF NOT EXISTS songs (
 id TEXT PRIMARY KEY, film_id TEXT NOT NULL REFERENCES films(id), title TEXT NOT NULL,
 source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS songs_film ON songs(film_id);
CREATE TABLE IF NOT EXISTS song_credits (
 song_id TEXT NOT NULL REFERENCES songs(id), person_id TEXT NOT NULL REFERENCES people(id),
 role TEXT NOT NULL, source_url TEXT NOT NULL,
 PRIMARY KEY(song_id,person_id,role,source_url)
);
CREATE TABLE IF NOT EXISTS import_jobs (
 source TEXT NOT NULL, item TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(source,item)
);
CREATE TABLE IF NOT EXISTS source_pages (
 url TEXT PRIMARY KEY, revision_url TEXT, license TEXT NOT NULL,
 retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, film_id TEXT NOT NULL REFERENCES films(id),
  kind TEXT NOT NULL, text TEXT NOT NULL, source_url TEXT NOT NULL,
  license TEXT NOT NULL DEFAULT '', retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(film_id, kind, source_url)
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  text, content='documents', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, text)
  VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, text)
  VALUES ('delete', old.id, old.text);
  INSERT INTO documents_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


class Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, factory=Connection)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if os.environ.get('FILMGRAPH_READONLY') == '1':
        con.execute('PRAGMA query_only=ON')  # hosted catalogue: any INSERT/UPDATE/DELETE/DDL now fails
    return con


def upsert_person(con: sqlite3.Connection, person: dict) -> str:
    pid = person["id"]
    identifiers = [pid, *person.get("identifiers", [])]
    matches = {pid} if con.execute('SELECT 1 FROM people WHERE id=?',(pid,)).fetchone() else set()
    for identifier in identifiers:
        known = con.execute("SELECT person_id FROM person_identifiers WHERE identifier=?", (identifier,)).fetchone()
        if known:
            matches.add(known[0])
        if con.execute('SELECT 1 FROM people WHERE id=?',(identifier,)).fetchone():
            matches.add(identifier)
    if matches:
        pid = min(matches, key=lambda p: (not bool(re.fullmatch(r'Q\d+', p)), p))
        for old in matches - {pid}:
            merge_person(con, old, pid)
    con.execute("INSERT INTO people(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (pid, person["name"]))
    for identifier in identifiers:
        con.execute("INSERT OR IGNORE INTO person_identifiers VALUES(?,?)", (identifier, pid))
    for alias in [person["name"], *person.get("aliases", [])]:
        if alias and normalize(alias):
            con.execute("INSERT OR IGNORE INTO aliases(person_id,alias,normalized) VALUES(?,?,?)",
                        (pid, alias, normalize(alias)))
    return pid


def merge_person(con, old, target):
    """Merge only identities connected by explicit external identifiers."""
    con.execute('INSERT OR IGNORE INTO aliases SELECT ?,alias,normalized FROM aliases WHERE person_id=?',(target,old))
    con.execute('DELETE FROM aliases WHERE person_id=?',(old,))
    for table in ('credits','credit_sources'):
        columns='film_id,person_id,role,character_name,source_url'
        con.execute(f'INSERT OR IGNORE INTO {table}({columns}) SELECT film_id,?,role,character_name,source_url FROM {table} WHERE person_id=?',(target,old))
        con.execute(f'DELETE FROM {table} WHERE person_id=?',(old,))
    con.execute('INSERT OR IGNORE INTO song_credits SELECT song_id,?,role,source_url FROM song_credits WHERE person_id=?',(target,old))
    con.execute('DELETE FROM song_credits WHERE person_id=?',(old,))
    con.execute('UPDATE person_identifiers SET person_id=? WHERE person_id=?',(target,old))
    con.execute('INSERT OR REPLACE INTO person_identifiers VALUES(?,?)',(old,target))
    con.execute('DELETE FROM people WHERE id=?',(old,))


def merge_film(con, old, target):
    for table, columns in (
        ('credits','person_id,role,character_name,source_url'),
        ('credit_sources','person_id,role,character_name,source_url'),
        ('facts','kind,value,source_url,evidence'),
        ('documents','kind,text,source_url,license'),
    ):
        con.execute(f'INSERT OR IGNORE INTO {table}(film_id,{columns}) SELECT ?,{columns} FROM {table} WHERE film_id=?',(target,old))
        con.execute(f'DELETE FROM {table} WHERE film_id=?',(old,))
    con.execute('UPDATE songs SET film_id=? WHERE film_id=?',(target,old))
    con.execute('UPDATE film_identifiers SET film_id=? WHERE film_id=?',(target,old))
    con.execute('INSERT OR REPLACE INTO film_identifiers VALUES(?,?)',(old,target))
    con.execute('DELETE FROM films WHERE id=?',(old,))


def import_records(con: sqlite3.Connection, records: list[dict]) -> int:
    """Upsert a normalized, source-attributed JSON dataset."""
    count = 0
    for original in records:
        film = dict(original)
        if film.get("language", "Malayalam").casefold() != "malayalam":
            continue
        # Only merge exact external identifiers, never a guessed title/year match.
        identifiers=[film['id'],*film.get('identifiers',[])]
        identifiers += [f['value'] for f in film.get('facts',[]) if f['kind']=='wikidata_id']
        if film.get('wikipedia_url'):
            identifiers.append('wiki:'+unquote(film['wikipedia_url'].split('/wiki/')[-1]).replace(' ','_'))
        if film.get('imdb_id'): identifiers.append('imdb:'+film['imdb_id'])
        matches=set()
        for identifier in identifiers:
            known=con.execute('SELECT film_id FROM film_identifiers WHERE identifier=?',(identifier,)).fetchone()
            if known: matches.add(known[0])
            if con.execute('SELECT 1 FROM films WHERE id=?',(identifier,)).fetchone():matches.add(identifier)
        match = con.execute("SELECT id FROM films WHERE id=? OR (wikipedia_url=? AND ? IS NOT NULL) "
                            "OR (imdb_id=? AND ? IS NOT NULL) ORDER BY id=? DESC LIMIT 1",
                            (film["id"], film.get("wikipedia_url"), film.get("wikipedia_url"),
                             film.get("imdb_id"), film.get("imdb_id"), film["id"])).fetchone()
        if match:
            matches.add(match['id'])
        if matches:
            film['id']=min(matches,key=lambda f:(not bool(re.fullmatch(r'Q\d+',f)),f))
            for old in matches-{film['id']}:merge_film(con,old,film['id'])
        con.execute(
            """INSERT INTO films(id,title,year,language,wikipedia_url,imdb_id)
            VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, year=COALESCE(excluded.year,films.year),
            wikipedia_url=COALESCE(excluded.wikipedia_url,films.wikipedia_url),
            imdb_id=COALESCE(excluded.imdb_id,films.imdb_id)""",
            (film["id"], film["title"], film.get("year"), "Malayalam",
             film.get("wikipedia_url"), film.get("imdb_id")),
        )
        for identifier in identifiers:
            con.execute('INSERT OR IGNORE INTO film_identifiers VALUES(?,?)',(identifier,film['id']))
        for credit in film.get("credits", []):
            person = credit["person"]
            pid = upsert_person(con, person)
            con.execute(
                """INSERT INTO credits(film_id,person_id,role,character_name,source_url)
                VALUES(?,?,?,?,?) ON CONFLICT(film_id,person_id,role,character_name)
                DO UPDATE SET source_url=excluded.source_url""",
                (film["id"], pid, credit["role"],
                 credit.get("character", ""), credit["source_url"]),
            )
            con.execute("INSERT OR IGNORE INTO credit_sources(film_id,person_id,role,character_name,source_url) VALUES(?,?,?,?,?)",
                        (film["id"], pid, credit["role"], credit.get("character", ""), credit["source_url"]))
        for fact in film.get("facts", []):
            con.execute("INSERT INTO facts(film_id,kind,value,source_url,evidence) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(film_id,kind,value,source_url) DO UPDATE SET evidence=excluded.evidence,retrieved_at=CURRENT_TIMESTAMP",
                        (film["id"], fact["kind"], str(fact["value"]), fact["source_url"], fact.get("evidence", "")))
        for song in film.get("songs", []):
            sid = film["id"] + ":song:" + normalize(song["title"])
            con.execute("INSERT INTO songs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,source_url=excluded.source_url",
                        (sid, film["id"], song["title"], song["source_url"]))
            for credit in song.get("credits", []):
                pid = upsert_person(con, credit["person"])
                con.execute("INSERT OR IGNORE INTO song_credits VALUES(?,?,?,?)",
                            (sid, pid, credit["role"], credit["source_url"]))
        for doc in film.get("documents", []):
            con.execute(
                """INSERT INTO documents(film_id,kind,text,source_url,license)
                VALUES(?,?,?,?,?) ON CONFLICT(film_id,kind,source_url)
                DO UPDATE SET text=excluded.text, license=excluded.license,
                retrieved_at=CURRENT_TIMESTAMP""",
                (film["id"], doc["kind"], doc["text"], doc["source_url"],
                 doc.get("license", "")),
            )
        count += 1
    con.commit()
    return count


def import_json(con: sqlite3.Connection, path: str | Path) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Import file must contain a JSON array of films")
    return import_records(con, data)
