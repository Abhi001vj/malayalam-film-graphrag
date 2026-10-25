import unittest
from filmgraph.catalogue import parse_list,parse_film

class CatalogueTests(unittest.TestCase):
    def test_historical_headers_and_rowspans(self):
        html='''<table class="wikitable"><tr><th>Opening</th><th>Film</th><th>Cast</th><th>Director</th></tr>
        <tr><td rowspan="2">January</td><td><a href="/wiki/Film_A">Film A</a></td><td><a href="/wiki/Actor_A">Actor A</a></td><td><a href="/wiki/Director_A">Director A</a></td></tr>
        <tr><td><a href="/wiki/Film_B">Film B</a></td><td><a href="/wiki/Actor_B">Actor B</a></td><td><a href="/wiki/Director_B">Director B</a></td></tr></table>'''
        records=parse_list(html,'https://en.wikipedia.org/wiki/List_of_Malayalam_films_of_1961',1961)
        self.assertEqual([r['title'] for r in records],['Film A','Film B'])
        self.assertEqual(records[1]['credits'][0]['person']['name'],'Actor B')

    def test_early_films_and_colon_in_film_title(self):
        html='''<table class="wikitable"><tr><th>Movies</th><th>Year</th><th>Director</th></tr><tr><td><a href="/wiki/1971:_Beyond_Borders">A: Film</a></td><td>1950</td><td>Director A</td></tr></table>'''
        result=parse_list(html,'https://en.wikipedia.org/wiki/List_of_Malayalam_films_before_1960')
        self.assertEqual(result[0]['year'],1950)
        self.assertIsNotNone(result[0]['wikipedia_url'])

    def test_lyrics_are_not_composer_and_song_edges_preserved(self):
        html='''<div class="mw-parser-output"><table class="infobox"><tr><th>Music by</th><td><b>Songs:</b><br><a href="/wiki/Composer_A">Composer A</a><br><b>Lyrics:</b><br><a href="/wiki/Writer_A">Writer A</a></td></tr></table>
        <h2>Soundtrack</h2><table><tr><th>Title</th><th>Lyrics</th><th>Singer(s)</th></tr><tr><td>Song A</td><td><a href="/wiki/Writer_A">Writer A</a></td><td><a href="/wiki/Singer_A">Singer A</a></td></tr></table></div>'''
        r=parse_film(html,{'id':'film:a','title':'Film A','wikipedia_url':'https://en.wikipedia.org/wiki/Film_A'})
        roles={(c['person']['name'],c['role']) for c in r['credits']}
        self.assertIn(('Writer A','lyricist'),roles)
        self.assertNotIn(('Writer A','composer'),roles)
        self.assertEqual(len(r['songs']),1)
        self.assertEqual({c['role'] for c in r['songs'][0]['credits']},{'singer','lyricist','composer'})

    def test_nested_cast_footnotes_do_not_become_actors(self):
        html='''<div class="mw-parser-output"><h2>Cast</h2><ul><li><a href="/wiki/Actor_A">Actor A</a> as Lead<ul><li><a href="/wiki/Voiceover">voiceover</a> detail</li></ul></li></ul></div>'''
        r=parse_film(html,{'id':'a','title':'A','wikipedia_url':'https://en.wikipedia.org/wiki/A'})
        self.assertEqual([c['person']['name'] for c in r['credits']],['Actor A'])

    def test_explicit_production_crew_clauses(self):
        html='''<div class="mw-parser-output"><h2>Production</h2><p><a href="/wiki/Person_A">Person A</a> and <a href="/wiki/Person_B">Person B</a> served as the second-unit directors. Choreography was done by <a href="/wiki/Person_C">Person C</a>.</p></div>'''
        r=parse_film(html,{'id':'a','title':'A','wikipedia_url':'https://en.wikipedia.org/wiki/A'})
        self.assertEqual({(c['person']['name'],c['role']) for c in r['credits']},{('Person A','second_unit_director'),('Person B','second_unit_director'),('Person C','choreographer')})
