"""Conservative Wikidata/Wikipedia importer for one release year at a time."""
from __future__ import annotations

import json
import time
from html.parser import HTMLParser
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .db import import_records

WDQS = "https://query.wikidata.org/sparql"
WIKI_API = "https://en.wikipedia.org/w/api.php"
PROPERTIES = {"P161": "actor", "P57": "director", "P162": "producer",
              "P58": "writer", "P86": "composer", "P1040": "editor", "P344": "cinematographer"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"style", "script", "sup"}:
            self.skip += 1
        if tag in {"p", "li", "br"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"style", "script", "sup"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _json(url: str, user_agent: str) -> dict:
    from .http import Client
    return Client(user_agent=user_agent).json(url)


def _sparql(query: str, user_agent: str) -> list[dict]:
    url = WDQS + "?" + urlencode({"query": query, "format": "json"})
    return _json(url, user_agent)["results"]["bindings"]


def _value(row: dict, field: str) -> str | None:
    return row.get(field, {}).get("value")


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _wikipedia_text(article_title: str, user_agent: str) -> list[dict]:
    page_url = "https://en.wikipedia.org/wiki/" + quote(article_title.replace(" ", "_"))
    base = {"action": "parse", "page": article_title, "format": "json"}
    section_data = _json(WIKI_API + "?" + urlencode({**base, "prop": "sections"}), user_agent)
    sections = section_data.get("parse", {}).get("sections", [])
    plot = next((s for s in sections if s.get("line", "").casefold() in
                 {"plot", "story", "synopsis"}), None)
    documents = []
    for kind, section in [("lead", "0"), ("plot", plot["index"] if plot else None)]:
        if section is None:
            continue
        data = _json(WIKI_API + "?" + urlencode({**base, "prop": "text", "section": section}), user_agent)
        html = data.get("parse", {}).get("text", {}).get("*", "")
        parser = _Text()
        parser.feed(html)
        text = " ".join(" ".join(parser.parts).split())[:12000]
        if text:
            documents.append({"kind": kind, "text": text, "source_url": page_url,
                              "license": "CC BY-SA; see source page for attribution and revision"})
        time.sleep(0.4)
    return documents


def import_year(con, year: int, user_agent: str, limit: int = 200,
                with_plots: bool = True) -> int:
    """Import a bounded year slice. Re-running upserts by Wikidata QID."""
    if not 1900 <= year <= 2100:
        raise ValueError("year must be between 1900 and 2100")
    if not user_agent or "(" not in user_agent or ")" not in user_agent:
        raise ValueError("Supply an identifying --user-agent with contact URL or email")
    limit = max(1, min(limit, 500))
    films_query = f"""
    SELECT DISTINCT ?film ?filmLabel ?date ?article ?imdb WHERE {{
      ?film wdt:P364 wd:Q36236; wdt:P577 ?date.
      FILTER(YEAR(?date) = {year})
      OPTIONAL {{ ?film wdt:P345 ?imdb. }}
      OPTIONAL {{ ?article schema:about ?film; schema:isPartOf <https://en.wikipedia.org/>. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,ml". }}
    }} LIMIT {limit}
    """
    rows = _sparql(films_query, user_agent)
    by_id: dict[str, dict] = {}
    for row in rows:
        fid = _qid(_value(row, "film"))
        article = _value(row, "article")
        by_id[fid] = {"id": fid, "title": _value(row, "filmLabel") or fid,
                      "year": year, "language": "Malayalam", "wikipedia_url": article,
                      "imdb_id": _value(row, "imdb"), "credits": [], "documents": []}
    ids = list(by_id)
    for start in range(0, len(ids), 20):
        batch = ids[start:start + 20]
        values = " ".join("wd:" + qid for qid in batch)
        props = " ".join("(wdt:" + prop + ' "' + role + '")' for prop, role in PROPERTIES.items())
        credits_query = f"""
        SELECT ?film ?person ?personLabel ?role WHERE {{
          VALUES ?film {{ {values} }}
          VALUES (?property ?role) {{ {props} }}
          ?film ?property ?person.
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,ml". }}
        }}
        """
        for row in _sparql(credits_query, user_agent):
            fid = _qid(_value(row, "film"))
            pid = _qid(_value(row, "person"))
            by_id[fid]["credits"].append({
                "person": {"id": pid, "name": _value(row, "personLabel") or pid},
                "role": _value(row, "role"), "source_url": "https://www.wikidata.org/wiki/" + fid,
            })
        time.sleep(0.5)
    if with_plots:
        for film in by_id.values():
            article = film["wikipedia_url"]
            if not article:
                continue
            title = article.rsplit("/", 1)[-1].replace("_", " ")
            try:
                film["documents"] = _wikipedia_text(title, user_agent)
            except (OSError, KeyError, ValueError) as exc:
                print(f"Skipping Wikipedia text for {title}: {exc}")
            time.sleep(0.5)
    import_records(con, list(by_id.values()))
    return len(by_id)
