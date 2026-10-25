import tempfile
import unittest
from pathlib import Path

from filmgraph import extract, nlu
from filmgraph.db import connect, import_records
from filmgraph.graphstore import SQLiteStore
from filmgraph.kg import build_kg
from filmgraph.retrieval import rrf

SRC = 'https://en.wikipedia.org/wiki/X'


def film(fid, title, year, credits):
    return {'id': fid, 'title': title, 'year': year, 'language': 'Malayalam', 'wikipedia_url': None,
            'credits': [{'person': {'id': pid, 'name': name}, 'role': role, 'source_url': SRC} for pid, name, role in credits]}


class GraphRAGTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.temp.name) / 'kg.sqlite3')
        import_records(self.con, [
            film('wiki:A', 'Alpha', 1990, [('wiki:Mohanlal', 'Mohanlal', 'actor'), ('wiki:Priyadarsan', 'Priyadarshan', 'director')]),
            film('wiki:B', 'Beta', 1995, [('Q1', 'Mohanlal', 'actor'), ('wiki:Priyadarsan', 'Priyadarshan', 'writer')]),
            film('Q2', 'Gamma', 2001, [('unresolved:1', 'Mohanlal', 'actor'), ('wiki:Sreenivasan', 'Sreenivasan', 'writer')]),
            film('list:9', 'Alpha', 1990, [('unresolved:2', 'Mohanlal', 'actor')]),  # duplicate film from a list page
            film('Q3', 'Delta', 2001, [('wiki:Mohanlal', 'Mohanlal', 'actor')]),
        ])
        # Q1 shares no film with wiki:Mohanlal, so link them through an explicit identifier.
        self.con.execute("INSERT OR REPLACE INTO person_identifiers VALUES('Q1','wiki:Mohanlal')")
        self.stats = build_kg(self.con)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_entity_resolution_merges_people_and_films(self):
        self.assertEqual(self.stats['films'], 4)  # list:9 merged into wiki:A by (title, year)
        ids = {r[0] for r in self.con.execute("SELECT canonical_id FROM kg_person_map WHERE person_id IN ('Q1','unresolved:1','unresolved:2')")}
        self.assertEqual(ids, {'wiki:Mohanlal'})
        self.assertEqual(self.con.execute("SELECT film_count FROM kg_people WHERE id='wiki:Mohanlal'").fetchone()[0], 4)

    def test_role_scoped_collaboration_counts(self):
        store = SQLiteStore(self.con)
        both, _ = store.films_with([{'id': 'wiki:Mohanlal', 'roles': []}, {'id': 'wiki:Priyadarsan', 'roles': []}])
        directed, _ = store.films_with([{'id': 'wiki:Mohanlal', 'roles': ['actor']}, {'id': 'wiki:Priyadarsan', 'roles': ['director']}])
        self.assertEqual(len(both), 2)
        self.assertEqual([r['title'] for r in directed], ['Alpha'])

    def test_shortest_path(self):
        path, _ = SQLiteStore(self.con).shortest_path('wiki:Priyadarsan', 'wiki:Sreenivasan')
        self.assertEqual(path[0], 'wiki:Priyadarsan')
        self.assertEqual(path[-1], 'wiki:Sreenivasan')
        self.assertEqual(len(path), 5)  # person-film-person-film-person


class NLUTests(unittest.TestCase):
    def test_time_slots(self):
        self.assertEqual(nlu.time_slots('films in the 90s'), {'year_from': 1990, 'year_to': 1999})
        self.assertEqual(nlu.time_slots('after 2010'), {'year_from': 2011})
        self.assertEqual(nlu.time_slots('how many between 1985 and 1995')['year_to'], 1995)

    def test_roles_bind_to_nearest_person(self):
        entities = [{'type': 'PERSON', 'name': 'A', 'start': 0, 'end': 1}, {'type': 'PERSON', 'name': 'B', 'start': 3, 'end': 4}]
        nlu.assign_roles('mohanlal actor and priyadarshan director', entities)
        self.assertEqual(entities[0]['roles'], ['actor'])
        self.assertEqual(entities[1]['roles'], ['director'])

    def test_by_binds_forward(self):
        entities = [{'type': 'PERSON', 'name': 'A', 'start': 3, 'end': 4}, {'type': 'PERSON', 'name': 'B', 'start': 6, 'end': 7}]
        nlu.assign_roles('songs sung by yesudas composed by johnson', entities)
        self.assertEqual(entities[0]['roles'], ['singer'])
        self.assertEqual(entities[1]['roles'], ['music_director'])

    def test_classifier_routes_delexicalised_text(self):
        clf = nlu.IntentClassifier()
        self.assertEqual(max(clf.predict('how many films did PERSON and PERSON do together').items(), key=lambda x: x[1])[0], 'collaboration')
        self.assertEqual(max(clf.predict('movie where a girl gets lost in a forest').items(), key=lambda x: x[1])[0], 'plot_search')


class ExtractionTests(unittest.TestCase):
    def test_link_anchored_rule_triples(self):
        html = b'''<div class="mw-parser-output"><p>Alpha is a 1990 film. It was a remake of
        <a href="/wiki/Old_Film">Old Film</a> and was shot in <a href="/wiki/Munnar">Munnar</a>.</p></div>'''
        mentions, infobox = extract.mentions_from_page(html, 'wiki:A', SRC)
        self.assertEqual({m[4] for m in mentions}, {'wiki:Old_Film', 'wiki:Munnar'})
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / 't.sqlite3')
            con.executescript(extract.SCHEMA)
            con.executemany('INSERT INTO kg_mentions VALUES(?,?,?,?,?,?,?)', mentions)
            con.executemany("INSERT INTO kg_entities VALUES(?,?,?,NULL,NULL,NULL)", [('wiki:Old_Film', 'Old Film', 'Film'), ('wiki:Munnar', 'Munnar', 'Place')])
            extract.apply_rules(con)
            got = {(r[0], r[1]) for r in con.execute('SELECT relation, object_id FROM kg_triples')}
            con.close()
        self.assertIn(('REMAKE_OF', 'wiki:Old_Film'), got)
        self.assertIn(('SHOT_AT', 'wiki:Munnar'), got)

    def test_film_relation_subject_is_the_preceding_linked_film(self):
        html = b'''<div class="mw-parser-output"><p>It followed <a href="/wiki/Chandramukhi">Chandramukhi</a>, which was a remake of
        the Malayalam film <a href="/wiki/Manichitrathazhu">Manichitrathazhu</a>.</p></div>'''
        mentions, _ = extract.mentions_from_page(html, 'wiki:Anandabhadram', SRC)
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / 't.sqlite3')
            con.executescript(extract.SCHEMA)
            con.executemany('INSERT INTO kg_mentions VALUES(?,?,?,?,?,?,?)', mentions)
            con.executemany("INSERT INTO kg_entities VALUES(?,?,?,NULL,NULL,NULL)", [('wiki:Chandramukhi', 'Chandramukhi', 'Film'), ('wiki:Manichitrathazhu', 'Manichitrathazhu', 'Film')])
            extract.apply_rules(con)
            got = {(r[0], r[1], r[2]) for r in con.execute("SELECT subject_id, relation, object_id FROM kg_triples WHERE relation='REMAKE_OF'")}
            con.close()
        self.assertEqual(got, {('wiki:Chandramukhi', 'REMAKE_OF', 'wiki:Manichitrathazhu')})

    def test_entity_typing(self):
        self.assertEqual(extract.classify({'Q5'}, ''), 'Person')
        self.assertEqual(extract.classify(set(), 'hill station in Idukki district, Kerala'), 'Place')
        self.assertEqual(extract.classify(set(), 'novel by M. T. Vasudevan Nair'), 'Work')


class FusionTests(unittest.TestCase):
    def test_rrf_rewards_agreement(self):
        fused = rrf([(1, .9), (2, .8)], [(2, 9.0), (3, 7.0)])
        self.assertEqual(fused[0][0], 2)


if __name__ == '__main__':
    unittest.main()
