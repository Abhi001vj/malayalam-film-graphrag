import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from filmgraph import feedback


class FeedbackTests(unittest.TestCase):
    def test_count_correction_needs_public_proof(self):
        base = {'kind': 'correction', 'question': 'mohanlal and mammootty movies', 'correction': 'Husbands in Goa has no Mammootty role'}
        with self.assertRaises(ValueError):
            feedback.validate({**base, 'claimed_value': '56'})
        for bad in ('http://example.com', 'https://localhost/x', 'https://127.0.0.1/x', 'javascript:alert(1)'):
            with self.assertRaises(ValueError):
                feedback.validate({**base, 'claimed_value': '56', 'proof_url': bad})
        ok = feedback.validate({**base, 'claimed_value': '56', 'proof_url': 'https://en.wikipedia.org/wiki/Husbands_in_Goa'})
        self.assertEqual(ok['claimed_value'], '56')

    def test_rating_values(self):
        with self.assertRaises(ValueError):
            feedback.validate({'kind': 'rating', 'question': 'q', 'rating': 5})
        self.assertEqual(feedback.validate({'kind': 'rating', 'question': 'q', 'rating': -1})['rating'], -1)

    def test_local_queue_and_review(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(feedback, 'PATH', feedback.Path(tmp) / 'f.sqlite3'), \
                patch('filmgraph.accounts.enabled', return_value=False), patch.dict(os.environ, {}, clear=False):
            handler = SimpleNamespace(headers={}, client_address=('127.0.0.1', 1))
            out = feedback.submit(handler, {'kind': 'correction', 'question': 'q', 'correction': 'count is off by two',
                                            'claimed_value': '59', 'proof_url': 'https://en.wikipedia.org/wiki/Mohanlal'})
            self.assertEqual(out['status'], 'pending')
            self.assertEqual(len(feedback.review()), 1)
            feedback.decide(out['id'], 'accepted', 'verified')
            self.assertEqual(feedback.review(), [])


if __name__ == '__main__':
    unittest.main()
