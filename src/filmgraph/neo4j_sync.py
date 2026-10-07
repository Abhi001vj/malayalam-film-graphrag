import sqlite3
import os
from neo4j import GraphDatabase

# Neo4j credentials
URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))

def sync_sqlite_to_neo4j(sqlite_db_path="data/films.sqlite3"):
    print("Starting sync from SQLite to Neo4j...")
    
    # 1. Connect to Neo4j
    try:
        driver = GraphDatabase.driver(URI, auth=AUTH)
        driver.verify_connectivity()
    except Exception as e:
        print(f"Error connecting to Neo4j. Is Docker running? {e}")
        return

    # 2. Connect to SQLite
    if not os.path.exists(sqlite_db_path):
        print(f"SQLite DB not found at {sqlite_db_path}")
        return
    
    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    with driver.session() as session:
        # Clear existing data
        print("Clearing Neo4j database...")
        session.run("MATCH (n) DETACH DELETE n")

        # Sync Films
        print("Syncing Films...")
        films = cursor.execute("SELECT * FROM films").fetchall()
        for film in films:
            session.run(
                "CREATE (f:Film {id: $id, title: $title, year: $year})",
                id=film["id"], title=film["title"], year=film["year"]
            )
        
        # Sync People
        print("Syncing People...")
        people = cursor.execute("SELECT * FROM people").fetchall()
        for person in people:
            session.run(
                "CREATE (p:Person {id: $id, name: $name, type: $type})",
                id=person["id"], name=person["name"], type=person["type"]
            )
        
        # Sync Credits (Relationships)
        print("Syncing Credits (Relationships)...")
        credits = cursor.execute("SELECT * FROM credits").fetchall()
        for credit in credits:
            # We map role to a relationship type (e.g. "actor" -> "ACTED_IN")
            rel_type = "ACTED_IN" if credit["role"] == "actor" else "WORKED_ON"
            session.run(
                f"""
                MATCH (p:Person {{id: $person_id}})
                MATCH (f:Film {{id: $film_id}})
                MERGE (p)-[r:{rel_type} {{role: $role}}]->(f)
                """,
                person_id=credit["person_id"], film_id=credit["film_id"], role=credit["role"]
            )

    driver.close()
    conn.close()
    print("Sync complete!")

if __name__ == "__main__":
    sync_sqlite_to_neo4j()
