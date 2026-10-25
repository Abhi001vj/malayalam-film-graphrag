import gzip
import json
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from filmgraph.db import connect,import_records
from filmgraph.search import search
from filmgraph.snapshot import snapshot

class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.con=connect(Path(self.temp.name)/'test.sqlite3')
        source='https://en.wikipedia.org/wiki/Test_Film'
        credits=[{'person':{'id':'person:a','name':'Actor A'},'role':'actor','source_url':source},
                 {'person':{'id':'person:b','name':'Writer B'},'role':'writer','source_url':source},
                 {'person':{'id':'person:b','name':'Writer B'},'role':'actor','source_url':source}]
        import_records(self.con,[{'id':'film:a','title':'Film A','year':1999,'credits':credits},
                                 {'id':'film:b','title':'Film B','year':2000,'credits':credits[:1]}])
    def tearDown(self):self.con.close();self.temp.cleanup()
    def test_roles_are_per_person_and_count_is_distinct(self):
        r=search(self.con,'How many movies with Actor A actor and Writer B writer?')
        self.assertEqual(r['total'],1)
        self.assertEqual([p['role'] for p in r['parsed']['people']],['actor','writer'])
        self.assertEqual(r['query_plan']['intent'],'collaboration_count')
    def test_validated_browser_plan(self):
        r=search(self.con,'test',query_plan={'version':1,'intent':'collaboration_count','people':[{'name':'Actor A','role':'actor'},{'name':'Writer B','role':'writer'}],'terms':[]})
        self.assertEqual(r['total'],1);self.assertEqual(r['parser'],'browser_templates')
    def test_generated_sql_is_not_an_allowed_intent(self):
        with self.assertRaises(ValueError):search(self.con,'test',query_plan={'version':1,'intent':'DROP TABLE films','people':[]})
        self.assertEqual(self.con.execute('select count(*) from films').fetchone()[0],2)
    def test_explicit_identifier_merge_preserves_sources(self):
        import_records(self.con,[{'id':'wiki:Film_A','title':'Film A','wikipedia_url':'https://en.wikipedia.org/wiki/Film_A','credits':[]}])
        import_records(self.con,[{'id':'Q100','title':'Film A','wikipedia_url':'https://en.wikipedia.org/wiki/Film_A','facts':[{'kind':'wikidata_id','value':'Q100','source_url':'https://www.wikidata.org/wiki/Q100'}]}])
        self.assertEqual(self.con.execute("select count(*) from films where wikipedia_url='https://en.wikipedia.org/wiki/Film_A'").fetchone()[0],1)
    def test_deployment_snapshot_removes_imdb_dataset_claims(self):
        import_records(self.con,[{'id':'film:a','title':'Film A','facts':[{'kind':'imdb_rating','value':'8.1','source_url':'https://www.imdb.com/title/tt1/'}]}])
        path=Path(self.temp.name)/'deploy.gz';snapshot(self.con,path)
        raw=Path(self.temp.name)/'read.sqlite';raw.write_bytes(gzip.decompress(path.read_bytes()))
        with closing(sqlite3.connect(raw)) as c:self.assertEqual(c.execute("select count(*) from facts where kind='imdb_rating'").fetchone()[0],0)
    def test_unrelated_person_alias_cannot_expand_canonical_name(self):
        self.con.execute("insert into people values('other','Other Name')")
        self.con.execute("insert into aliases values('other','Actor A','actor a')");self.con.commit()
        self.assertNotIn('other',search(self.con,'Actor A')['parsed']['people'][0]['ids'])
