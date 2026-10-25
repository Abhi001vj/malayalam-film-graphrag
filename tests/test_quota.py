import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from filmgraph.accounts import consume_quota

class QuotaTests(unittest.TestCase):
    def test_daily_cap_is_atomic_and_repeats_are_free(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'FILMGRAPH_ACCOUNTS_DB':temp+'/accounts.sqlite3','FILMGRAPH_LOCAL_GUEST_LIMIT':'10'}),patch('filmgraph.accounts.enabled',return_value=False):
            handler=SimpleNamespace(headers={},client_address=('127.0.0.1',123))
            with ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(lambda i:consume_quota(handler,'Question '+str(i),{}),range(20)))
            self.assertEqual(sum(r['allowed'] for r in results),10)
            allowed=next(i for i,r in enumerate(results) if r['allowed'])
            repeat=consume_quota(handler,'Question '+str(allowed),{})
            self.assertTrue(repeat['allowed']);self.assertEqual(repeat['used'],10)
