import unittest

from filmgraph.appearance import classify, decide
from filmgraph.pipeline import acting_bucket


class AppearanceTest(unittest.TestCase):
    def test_qualifiers_from_real_cast_lines(self):
        self.assertEqual(classify('the narrator (voice only)'), 'voice')
        self.assertEqual(classify('Mohanlal (television scene)'), 'archive')
        self.assertEqual(classify('Adv. Nandagopal Marar (cameo appearance)'), 'cameo')
        self.assertEqual(classify('a fictionalized version of himself (extended cameo appearance)'), 'self')
        self.assertEqual(classify('Jagadeesh Nair(Guest Appearance)'), 'cameo')
        self.assertIsNone(classify('Barroz, a Malabari slave to Cristóvão da Gama'))

    def test_named_role_beats_bare_claims_and_qualifier_beats_bare(self):
        page = 'https://en.wikipedia.org/wiki/X'
        self.assertEqual(decide([('role', '', 'wikidata', False), ('role', 'as Barroz', page, True)])[0], 'role')
        self.assertEqual(decide([('role', '', 'wikidata', False), ('voice', 'Infobox: Narrated by', page, False)])[0], 'voice')
        self.assertEqual(decide([('role', 'Mohanlal (opening)', page, False), ('voice', 'Infobox: Narrated by', page, False)])[0], 'voice')
        self.assertEqual(decide([])[0], 'role')

    def test_acting_together_needs_real_roles_for_everyone(self):
        role = {'appearance': {'kind': 'role'}}
        ev = lambda a, b, ok=True: {'confirmed': ok, 'people': {'A': a, 'B': b}}
        self.assertEqual(acting_bucket(ev(role, role)), 'costar')
        self.assertEqual(acting_bucket(ev(role, {'appearance': {'kind': 'cameo'}})), 'cameo')
        self.assertEqual(acting_bucket(ev(role, {'appearance': {'kind': 'voice'}})), 'offscreen')
        self.assertEqual(acting_bucket(ev(role, role, ok=False)), 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
