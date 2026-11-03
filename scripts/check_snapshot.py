"""Compare a hosting snapshot with the full local graph before deploying it.

The snapshot removes IMDb-only credits on purpose, so small drops are expected. Large drops for well-known
people mean identities were split or renamed (see the Priyadarshan / Ouseppachan regression, Nov 2026).

    python scripts/check_snapshot.py                       # data/films.sqlite3 vs data/deploy.sqlite3.gz
    python scripts/check_snapshot.py --top 500 --min-kept 0.9

Exit code 1 when any of the top person-role pairs keeps less than --min-kept of its credits.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from filmgraph.db import normalize  # noqa: E402


def credits_by_alias(con: sqlite3.Connection, name: str, role: str) -> int:
    """Largest credit count among canonical people reachable from this name through the alias table."""
    key = normalize(name)
    row = con.execute(
        """SELECT max(n) FROM (
             SELECT count(DISTINCT c.film_id) n FROM kg_credits c
             WHERE c.role=? AND c.person_id IN (
               SELECT m.canonical_id FROM kg_person_map m
               WHERE m.person_id IN (SELECT person_id FROM aliases WHERE normalized=?)
                  OR m.canonical_id IN (SELECT id FROM kg_people WHERE name=?))
             GROUP BY c.person_id)""", (role, key, name)).fetchone()
    return row[0] or 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--full', default='data/films.sqlite3')
    ap.add_argument('--snapshot', default='data/deploy.sqlite3.gz')
    ap.add_argument('--top', type=int, default=300)
    ap.add_argument('--min-kept', type=float, default=0.85)
    args = ap.parse_args()

    full = sqlite3.connect(args.full)
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'snapshot.sqlite3'
        with gzip.open(args.snapshot, 'rb') as src, path.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        snap = sqlite3.connect(path)

        totals = [('films', 'kg_films'), ('people', 'kg_people'), ('credits', 'kg_credits')]
        for label, table in totals:
            f = full.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            s = snap.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            print(f'{label:8} full {f:>7}  snapshot {s:>7}  ({(s - f) / f * 100:+.1f}%)')

        top = full.execute(
            """SELECT p.name, c.role, count(DISTINCT c.film_id) n FROM kg_credits c JOIN kg_people p ON p.id=c.person_id
               GROUP BY p.id, c.role ORDER BY n DESC LIMIT ?""", (args.top,)).fetchall()
        failures = []
        for name, role, n in top:
            kept = credits_by_alias(snap, name, role)
            if kept < args.min_kept * n:
                failures.append((name, role, n, kept))
        print(f'{len(top) - len(failures)}/{len(top)} top person-role pairs keep >= {args.min_kept:.0%} of their credits')
        for name, role, n, kept in failures:
            print(f'  LOW  {name} ({role}): {n} -> {kept}')
        snap.close()
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
