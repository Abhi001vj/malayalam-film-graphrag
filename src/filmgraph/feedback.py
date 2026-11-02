"""User ratings and corrections with proof: the human-in-the-loop data-quality queue.

Every answer can be rated (up/down) and corrected ("it's 59, not 57" + a proof link). Nothing a user
submits changes the graph; corrections wait in a review queue. A curator accepts or rejects them
(`filmgraph feedback --review`); accepted ones become regression cases in evals/ and data fixes.
Local runs store in data/feedback.sqlite3; hosted runs go through the signed Supabase gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

from . import accounts

PATH = Path(os.environ.get('FILMGRAPH_FEEDBACK_DB', 'data/feedback.sqlite3'))


def _db() -> Path:
    """The review-queue database: the settings page value when one is saved, else PATH."""
    from . import settings
    return Path(settings.saved().get('paths', {}).get('feedback_db') or PATH)
KINDS = {'rating', 'correction'}
# Proof must point at something a curator can check; social posts are fine, but no local or raw IP URLs.
ALLOWED_PROOF = re.compile(r'^https://[a-z0-9.-]+\.[a-z]{2,}(/|$)', re.I)
SCHEMA = """CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, rating INTEGER,
  question TEXT NOT NULL, intent TEXT, answer_excerpt TEXT, entities TEXT, film_ids TEXT,
  claimed_value TEXT, correction TEXT, proof_url TEXT, reporter TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', reviewer_note TEXT)"""


def validate(body: dict) -> dict:
    kind = body.get('kind')
    if kind not in KINDS:
        raise ValueError('kind must be rating or correction')
    question = body.get('question')
    if not isinstance(question, str) or not 1 <= len(question) <= 500:
        raise ValueError('question is required')
    clean = {'kind': kind, 'question': question.strip(), 'intent': str(body.get('intent') or '')[:40],
             'answer_excerpt': str(body.get('answer') or '')[:600],
             'entities': [str(e)[:120] for e in (body.get('entities') or [])][:8],
             'film_ids': [str(f)[:300] for f in (body.get('film_ids') or [])][:60]}
    if kind == 'rating':
        if body.get('rating') not in (1, -1):
            raise ValueError('rating must be 1 or -1')
        clean['rating'] = body['rating']
        clean['correction'] = str(body.get('comment') or '')[:1000]
        return clean
    correction = body.get('correction')
    if not isinstance(correction, str) or not 5 <= len(correction.strip()) <= 2000:
        raise ValueError('Describe the correction (5–2000 characters)')
    proof = str(body.get('proof_url') or '').strip()
    claimed = str(body.get('claimed_value') or '').strip()[:100]
    host = urlparse(proof).hostname or ''
    if proof and (not ALLOWED_PROOF.match(proof) or len(proof) > 500 or host in {'localhost'} or re.match(r'^[\d.]+$', host)):
        raise ValueError('Proof must be a public https link (e.g. a Wikipedia page, review or interview)')
    if claimed and not proof:
        raise ValueError('A corrected number or name needs a proof link so a curator can verify it')
    clean.update(correction=correction.strip(), proof_url=proof or None, claimed_value=claimed or None)
    return clean


def submit(handler, body: dict) -> dict:
    item = validate(body)
    ip = handler.headers.get('x-vercel-forwarded-for', '').split(',')[0].strip() if os.environ.get('VERCEL') else handler.client_address[0]
    ip = ip or handler.client_address[0]
    if accounts.enabled():
        import hmac
        guest = hmac.new(accounts.SECRET.encode(), ip.encode(), hashlib.sha256).hexdigest()
        return accounts.gateway({'action': 'feedback', 'access_token': accounts.access_token(handler) or None,
                                 'guest_key': guest, 'item': item})
    reporter = 'guest:' + hashlib.sha256(('feedback:' + ip).encode()).hexdigest()[:16]  # pseudonymous, no raw IP
    _db().parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(_db(), timeout=30)) as con:
        con.execute(SCHEMA)
        recent = con.execute("SELECT count(*) FROM feedback WHERE reporter=? AND created_at > datetime('now','-1 hour')", (reporter,)).fetchone()[0]
        if recent >= 30:
            raise ValueError('Too much feedback from this network in the last hour; please try later')
        cur = con.execute("""INSERT INTO feedback(created_at,kind,rating,question,intent,answer_excerpt,entities,film_ids,
                             claimed_value,correction,proof_url,reporter) VALUES(datetime('now'),?,?,?,?,?,?,?,?,?,?,?)""",
                          (item['kind'], item.get('rating'), item['question'], item['intent'], item['answer_excerpt'],
                           json.dumps(item['entities'], ensure_ascii=False), json.dumps(item['film_ids']),
                           item.get('claimed_value'), item.get('correction'), item.get('proof_url'), reporter))
        con.commit()
        return {'stored': True, 'id': cur.lastrowid, 'status': 'pending' if item['kind'] == 'correction' else 'recorded'}


def review(status: str = 'pending', limit: int = 50) -> list[dict]:
    if not _db().exists():
        return []
    with closing(sqlite3.connect(_db())) as con:
        con.row_factory = sqlite3.Row
        con.execute(SCHEMA)
        return [dict(r) for r in con.execute('SELECT * FROM feedback WHERE kind=? AND status=? ORDER BY id DESC LIMIT ?',
                                             ('correction', status, limit))]


def decide(item_id: int, status: str, note: str = '') -> None:
    if status not in ('accepted', 'rejected'):
        raise ValueError('status must be accepted or rejected')
    with closing(sqlite3.connect(_db())) as con:
        con.execute('UPDATE feedback SET status=?, reviewer_note=? WHERE id=?', (status, note, item_id))
        con.commit()


def summary() -> dict:
    if not _db().exists():
        return {'ratings': {}, 'corrections': {}}
    with closing(sqlite3.connect(_db())) as con:
        con.execute(SCHEMA)
        ratings = dict(con.execute("SELECT intent || ':' || rating, count(*) FROM feedback WHERE kind='rating' GROUP BY 1"))
        corrections = dict(con.execute("SELECT status, count(*) FROM feedback WHERE kind='correction' GROUP BY 1"))
    return {'ratings_by_intent': ratings, 'corrections': corrections, 'generated': time.strftime('%Y-%m-%d %H:%M')}
