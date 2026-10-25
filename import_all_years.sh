#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/filmgraph import-catalogue --from-year 1928 --to-year "${END_YEAR:-2026}"
.venv/bin/filmgraph import-wikidata
.venv/bin/filmgraph enrich-pages --workers 4
.venv/bin/filmgraph stats
