"""Runtime settings: data paths, models, sources and relations.

Defaults are exactly what the app used before this page existed: environment variables
(see .env.example), then the built-in values. The settings page saves only the values that
differ from those defaults to data/settings.json (FILMGRAPH_SETTINGS), so clearing a field
or pressing "Reset" falls back to the default again. Secrets (Neo4j password, API keys,
Slack webhook) stay in the environment and are never shown or saved here.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SOURCES = ['wikipedia', 'wikidata', 'imdb']
# Prose triples (kg_triples / Neo4j typed edges). MENTIONS is the weak unlabelled link.
RELATIONS = ['REMAKE_OF', 'REMADE_AS', 'SEQUEL_TO', 'HAS_SEQUEL', 'BASED_ON', 'SHOT_AT', 'SET_IN', 'PRODUCED_BY',
             'DISTRIBUTED_BY', 'WON_AWARD', 'NOMINATED_FOR', 'SCREENED_AT', 'INSPIRED_BY', 'DEBUTED_IN', 'REFERENCES', 'MENTIONS']
PATH_ROLES = ['actor', 'director', 'writer', 'producer', 'cinematographer', 'music_director', 'editor', 'lyricist',
              'singer', 'production_designer', 'choreographer', 'costume_designer', 'sound_designer']

_runtime: dict = {}  # defaults only known at startup, e.g. the --db the server was started with


def defaults() -> dict:
    env = os.getenv
    return {
        'paths': {
            'db': _runtime.get('db', 'data/films.sqlite3'),
            'vectors_dir': 'data/vectors',
            'feedback_db': env('FILMGRAPH_FEEDBACK_DB', 'data/feedback.sqlite3'),
            'accounts_db': env('FILMGRAPH_ACCOUNTS_DB', 'data/accounts.sqlite3'),
            'traces_log': 'data/traces.jsonl',
            'handover_log': 'data/handover.jsonl',
        },
        'models': {
            'llm_provider': env('FILMGRAPH_LLM', 'ollama'),
            'chat_model': env('FILMGRAPH_LLM_MODEL', 'gemma4-e4b-32k:latest'),
            'embed_model': env('FILMGRAPH_EMBED_MODEL', 'embeddinggemma'),
            'ollama_host': env('OLLAMA_HOST', 'http://127.0.0.1:11434'),
            'llm_base_url': env('FILMGRAPH_LLM_BASE_URL', ''),
            'graph_backend': env('FILMGRAPH_GRAPH', 'auto'),
            'neo4j_uri': env('NEO4J_URI', ''),
            'neo4j_username': env('NEO4J_USERNAME', 'neo4j'),
            'neo4j_database': env('NEO4J_DATABASE', 'neo4j'),
        },
        'sources': {
            'enabled': list(SOURCES),          # credits from other sources are ignored
            'confirming': ['wikipedia'],       # a credit on one of these counts as ✓ confirmed
        },
        'relations': {
            'enabled': [r for r in RELATIONS if r != 'MENTIONS'],
            'path_roles': PATH_ROLES[:8],      # credits that connection paths may walk through
        },
    }


OPTIONS = {
    'models.llm_provider': ['ollama', 'openai', 'none'],
    'models.graph_backend': ['auto', 'neo4j', 'sqlite'],
    'sources.enabled': SOURCES, 'sources.confirming': SOURCES,
    'relations.enabled': RELATIONS, 'relations.path_roles': PATH_ROLES,
}


def _file() -> Path:
    return Path(os.getenv('FILMGRAPH_SETTINGS', 'data/settings.json'))


_cache: dict = {'mtime': None, 'data': {}}


def saved() -> dict:
    """Overrides from the settings file, re-read only when the file changes."""
    try:
        mtime = _file().stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache['mtime']:
        try:
            _cache['data'] = json.loads(_file().read_text())
        except (OSError, ValueError):
            _cache['data'] = {}
        _cache['mtime'] = mtime
    return _cache['data']


def current() -> dict:
    out = defaults()
    for group, values in saved().items():
        if group in out and isinstance(values, dict):
            out[group].update({k: v for k, v in values.items() if k in out[group]})
    return out


def get(key: str):
    """get('models.chat_model') -> saved override, else the default."""
    group, name = key.split('.', 1)
    return current()[group][name]


def writable() -> bool:
    return not os.getenv('VERCEL')


def _validate(group: str, name: str, value, defaults_: dict):
    default = defaults_[group][name]
    key = f'{group}.{name}'
    if isinstance(default, list):
        if not isinstance(value, list) or any(v not in OPTIONS[key] for v in value):
            raise ValueError(f'{key}: choose from {", ".join(OPTIONS[key])}')
        if not value and key in ('sources.enabled', 'sources.confirming', 'relations.path_roles'):
            raise ValueError(f'{key}: select at least one')
        return [v for v in OPTIONS[key] if v in value]
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError(f'{key}: expected text up to 500 characters')
    value = value.strip()
    if key in OPTIONS and value not in OPTIONS[key]:
        raise ValueError(f'{key}: choose from {", ".join(OPTIONS[key])}')
    if key in ('models.ollama_host', 'models.llm_base_url') and value and not value.startswith(('http://', 'https://')):
        raise ValueError(f'{key}: must start with http:// or https://')
    if key == 'models.neo4j_uri' and value and not value.startswith(('bolt://', 'bolt+s://', 'neo4j://', 'neo4j+s://')):
        raise ValueError(f'{key}: must be a bolt:// or neo4j:// URI')
    if key == 'paths.db' and value != default:
        try:
            with sqlite3.connect(f'file:{value}?mode=ro', uri=True) as con:
                con.execute('SELECT 1 FROM kg_films LIMIT 1')
        except sqlite3.Error:
            raise ValueError(f'{key}: {value} is not a FilmGraph database (needs kg_films; run filmgraph build-kg)')
    return value


def save(values: dict) -> dict:
    """Validate and store only the values that differ from the defaults."""
    if not writable():
        raise PermissionError('Settings are read-only on the hosted deployment')
    base = defaults()
    overrides = {}
    for group, items in (values or {}).items():
        if group not in base or not isinstance(items, dict):
            raise ValueError(f'Unknown settings group: {group}')
        for name, value in items.items():
            if name not in base[group]:
                raise ValueError(f'Unknown setting: {group}.{name}')
            value = _validate(group, name, value, base)
            if value != base[group][name] and value != '':
                overrides.setdefault(group, {})[name] = value
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(overrides, indent=2) + '\n')
    tmp.replace(path)
    _changed()
    return overrides


def _changed() -> None:
    """Drop caches that depend on settings so a save applies to the next request."""
    from . import llm, graphstore, retrieval
    llm.available.cache_clear()
    graphstore.reset_driver()
    retrieval._index.cache_clear()


def counts(con: sqlite3.Connection) -> dict:
    """How much of the graph each source / relation / credit role covers, shown beside each option."""
    try:
        return {
            'sources': {s: con.execute("SELECT count(*) FROM kg_credits WHERE ','||sources||',' LIKE ?", (f'%,{s},%',)).fetchone()[0]
                        for s in SOURCES},
            'relations': dict(con.execute('SELECT relation, count(*) FROM kg_triples GROUP BY relation').fetchall()),
            'path_roles': dict(con.execute('SELECT role, count(*) FROM kg_credits GROUP BY role').fetchall()),
        }
    except sqlite3.Error:
        return {}


def describe(con: sqlite3.Connection | None = None) -> dict:
    return {'defaults': defaults(), 'current': current(), 'saved': saved(), 'options': OPTIONS,
            'writable': writable(), 'file': str(_file()), 'counts': counts(con) if con is not None else {}}
