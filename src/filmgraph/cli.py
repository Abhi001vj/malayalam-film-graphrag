"""Command line entry point."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import connect, import_json
from .search import search, coverage
from .server import serve
from .wikimedia import import_year

DEFAULT_DB = Path("data/films.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(description="Malayalam film GraphRAG prototype")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import-json", help="Import normalized, sourced film records")
    load.add_argument("path", type=Path)
    wiki = sub.add_parser("import-wikimedia", help="Import one year from Wikimedia APIs")
    wiki.add_argument("--year", type=int, required=True)
    wiki.add_argument("--user-agent", required=True, help="App/version (contact URL or email)")
    wiki.add_argument("--limit", type=int, default=200)
    wiki.add_argument("--no-plots", action="store_true")
    catalog = sub.add_parser("import-catalogue", help="Import all Wikipedia year lists, resumably")
    catalog.add_argument("--from-year", type=int, default=1928)
    catalog.add_argument("--to-year", type=int, default=2026)
    catalog.add_argument("--refresh", action="store_true")
    pages = sub.add_parser("enrich-pages", help="Enrich all discovered Wikipedia film pages")
    pages.add_argument("--limit", type=int)
    pages.add_argument("--workers", type=int, default=4)
    pages.add_argument("--refresh", action="store_true")
    sub.add_parser("import-wikidata", help="Discover all Wikidata Malayalam films and enrich structured claims")
    imdb = sub.add_parser("import-imdb", help="Enrich linked titles from official personal/noncommercial IMDb files")
    imdb.add_argument("--directory", type=Path, default=Path("data/imdb"))
    imdb.add_argument("--no-principals", action="store_true")
    snap = sub.add_parser("snapshot", help="Create a compact serving snapshot, excluding IMDb claims by default")
    snap.add_argument("--output", type=Path, default=Path("data/deploy.sqlite3.gz"))
    snap.add_argument("--include-imdb", action="store_true", help="Personal/noncommercial snapshots only")
    sub.add_parser("stats", help="Report source and field coverage")
    query = sub.add_parser("ask", help="Search from the terminal")
    query.add_argument("question")
    query.add_argument("--vectors", action="store_true", help="Use built semantic vectors")
    sub.add_parser("build-vectors", help="Embed story documents using the optional model")
    sub.add_parser("build-kg", help="Entity resolution -> canonical kg_* tables")
    sub.add_parser("build-appearances", help="Classify acting credits: role / cameo / self / voice / archive")
    sub.add_parser("build-chunks", help="Chunk lead/plot/production text into the lexical graph")
    sub.add_parser("embed", help="Embed chunks with the local Ollama embedding model")
    tri = sub.add_parser("extract-triples", help="Link-anchored triple extraction from cached film pages")
    tri.add_argument("--stage", choices=["all", "mentions", "types", "rules", "llm"], default="all")
    tri.add_argument("--llm-films", type=int, default=200, help="films to relabel with the local LLM")
    comm = sub.add_parser("communities", help="Louvain communities + (LLM) community reports")
    comm.add_argument("--summaries", type=int, default=40)
    neo = sub.add_parser("load-neo4j", help="Load the KG into Neo4j (NEO4J_URI/USERNAME/PASSWORD)")
    neo.add_argument("--keep", action="store_true", help="do not wipe the database first")
    chat = sub.add_parser("chat", help="Interactive GraphRAG chat in the terminal")
    chat.add_argument("--no-llm", action="store_true")
    chat.add_argument("--backend", choices=["auto", "neo4j", "sqlite"], default="auto")
    fb = sub.add_parser("feedback", help="Ratings summary and the correction review queue")
    fb.add_argument("--accept", type=int, metavar="ID")
    fb.add_argument("--reject", type=int, metavar="ID")
    fb.add_argument("--note", default="")
    web = sub.add_parser("serve", help="Run local browser UI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--vectors", action="store_true", help="Use built semantic vectors")
    args = parser.parse_args()
    if args.command == "feedback":
        from . import feedback
        if args.accept or args.reject:
            feedback.decide(args.accept or args.reject, "accepted" if args.accept else "rejected", args.note)
        print(json.dumps(feedback.summary(), indent=2))
        for item in feedback.review():
            print(f"\n#{item['id']} [{item['created_at']}] Q: {item['question']}\n  answer: {item['answer_excerpt'][:160]}"
                  f"\n  claims: {item['claimed_value']}  proof: {item['proof_url']}\n  says: {item['correction']}")
        return
    if args.command == "serve":
        serve(str(args.db), args.host, args.port, args.vectors)
        return
    with connect(args.db) as con:
        if args.command == "import-json":
            print(f"Imported {import_json(con, args.path)} film records")
        elif args.command == "import-wikimedia":
            count = import_year(con, args.year, args.user_agent, args.limit,
                                not args.no_plots)
            print(f"Imported {count} Wikimedia film records")
        elif args.command == "import-catalogue":
            from .catalogue import import_catalogue
            from .http import Client
            print(f"Imported {import_catalogue(con, Client(), args.from_year, args.to_year, args.refresh)} catalogue records")
        elif args.command == "enrich-pages":
            from .catalogue import enrich_pages
            from .http import Client
            print(f"Enriched {enrich_pages(args.db, Client(), args.limit, args.workers, args.refresh)} film pages")
        elif args.command == "import-wikidata":
            from .wikidata_catalogue import discover, enrich
            from .http import Client
            client=Client()
            discover(con, client)
            enrich(con, client)
        elif args.command == "import-imdb":
            from .imdb import import_imdb
            print(f"Enriched {import_imdb(con,args.directory,not args.no_principals)} IMDb titles")
        elif args.command == "snapshot":
            from .snapshot import snapshot
            print(json.dumps(snapshot(con,args.output,args.include_imdb),indent=2))
        elif args.command == "stats":
            print(json.dumps(coverage(con),ensure_ascii=False,indent=2))
        elif args.command == "build-kg":
            from .kg import build_kg
            print(json.dumps(build_kg(con), indent=2))
        elif args.command == "build-appearances":
            from .appearance import build_appearances
            print(json.dumps(build_appearances(con), indent=2))
        elif args.command == "build-chunks":
            from .retrieval import build_chunks
            print(f"{build_chunks(con)} chunks")
        elif args.command == "embed":
            from .retrieval import build_embeddings
            print(f"Embedded {build_embeddings(con)} chunks")
        elif args.command == "extract-triples":
            from . import extract
            if args.stage in ("all", "mentions"): print(f"{extract.extract_mentions(con)} mentions")
            if args.stage in ("all", "types"): print(extract.type_entities(con))
            if args.stage in ("all", "rules"): print(f"{extract.apply_rules(con)} rule/mention triples")
            if args.stage in ("all", "llm"): print(extract.llm_label(con, args.llm_films))
        elif args.command == "communities":
            from .communities import detect, summarise
            print(detect(con)); print(f"{summarise(con, args.summaries)} LLM community reports")
        elif args.command == "load-neo4j":
            import os
            from neo4j import GraphDatabase
            from .graphdb import load
            driver = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                                          auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "filmgraph-local")))
            print(json.dumps(load(con, driver, wipe=not args.keep), indent=2))
        elif args.command == "chat":
            from .pipeline import answer
            context = None
            while True:
                try:
                    q = input("\nyou> ").strip()
                except EOFError:
                    break
                if not q:
                    continue
                r = answer(con, q, context, use_llm=not args.no_llm, backend=args.backend)
                context = r["context"]
                print(f"[{r['intent']} · {r['method']} {r['confidence']} · {r['backend']} · {r['total_ms']} ms]\n{r['answer']}")
                if r.get("clarify"):
                    print("options:", ", ".join(o["name"] + " = " + o["id"] for o in r["clarify"]["options"]))
        elif args.command == "build-vectors":
            from .vectors import build_vectors
            print(f"Embedded {build_vectors(con)} story documents")
        else:
            print(json.dumps(search(con, args.question, use_vectors=args.vectors),
                             ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
