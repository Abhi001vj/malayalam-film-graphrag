"""Protections for the public, read-only hosted demo."""
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from filmgraph import server
from filmgraph.accounts import AccountError, consume_quota
from filmgraph.db import connect


class HostedSafetyTests(unittest.TestCase):
    def test_public_demo_limits_requests_per_minute(self):
        env = {'VERCEL': '1', 'FILMGRAPH_PUBLIC_DEMO': '1', 'FILMGRAPH_GUEST_PER_MINUTE': '3'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {**env, 'FILMGRAPH_ACCOUNTS_DB': temp + '/a.sqlite3'}), \
                patch('filmgraph.accounts.enabled', return_value=False):
            handler = SimpleNamespace(headers={'x-vercel-forwarded-for': '203.0.113.7'}, client_address=('10.0.0.1', 1))
            results = [consume_quota(handler, 'same question', {}) for _ in range(5)]
            self.assertEqual([r['allowed'] for r in results], [True, True, True, False, False])
            self.assertEqual(results[-1]['reason'], 'minute')
            self.assertGreater(results[-1]['retry_after'], 0)
            other = SimpleNamespace(headers={'x-vercel-forwarded-for': '198.51.100.9'}, client_address=('10.0.0.1', 1))
            self.assertTrue(consume_quota(other, 'same question', {})['allowed'])  # limits are per visitor

    def test_hosted_without_gateway_or_demo_flag_refuses(self):
        with patch.dict(os.environ, {'VERCEL': '1'}), patch('filmgraph.accounts.enabled', return_value=False):
            os.environ.pop('FILMGRAPH_PUBLIC_DEMO', None)
            handler = SimpleNamespace(headers={}, client_address=('10.0.0.1', 1))
            with self.assertRaises(AccountError):
                consume_quota(handler, 'q', {})

    def test_read_only_catalogue_rejects_writes(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'FILMGRAPH_READONLY': '1'}):
            con = connect(temp + '/films.sqlite3')
            con.execute('SELECT count(*) FROM films').fetchone()
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM films")
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DROP TABLE films")
            con.close()

    def test_public_settings_hide_paths_and_hosts(self):
        info = {'file': '/Users/me/settings.json', 'defaults': {'paths': {'db': '/data/films.sqlite3'},
                'models': {'ollama_host': 'http://127.0.0.1:11434', 'neo4j_uri': 'bolt://x', 'chat_model': 'gemma'}},
                'current': {}, 'saved': {}}
        shown = server.public_settings(info)
        text = repr(shown)
        for secret in ('/Users/me', '/data/films', '127.0.0.1', 'bolt://'):
            self.assertNotIn(secret, text)
        self.assertEqual(shown['defaults']['models']['chat_model'], 'gemma')  # harmless fields stay
        self.assertEqual(info['file'], '/Users/me/settings.json')  # original untouched

    def test_bounded_integers(self):
        self.assertEqual(server.bounded('1000000', 20, 1, 50), 50)
        self.assertEqual(server.bounded('-5', 0, 0, 5000), 0)
        self.assertEqual(server.bounded('abc', 20, 1, 50), 20)
        self.assertEqual(server.bounded(None, 20, 1, 50), 20)

    def test_api_throttle_per_ip(self):
        with patch.object(server, 'API_PER_MINUTE', 3), patch.dict(server._hits, clear=True):
            waits = [server.throttled('192.0.2.1') for _ in range(5)]
            self.assertEqual(waits[:3], [0, 0, 0])
            self.assertTrue(all(w > 0 for w in waits[3:]))
            self.assertEqual(server.throttled('192.0.2.2'), 0)


if __name__ == '__main__':
    unittest.main()
