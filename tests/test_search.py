import tempfile
import unittest
from pathlib import Path

from filmgraph.db import connect, import_json
from filmgraph.search import search


EXAMPLES = Path(__file__).resolve().parents[1] / "data" / "example_films.json"


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.temp.name) / "test.sqlite3")
        import_json(self.con, EXAMPLES)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_cast_year_and_story_select_correct_film(self):
        result = search(self.con, "Jayaram and Jagathy acted in 1991, husband hires killers")
        self.assertEqual(result["results"][0]["title"], "Kadinjool Kalyanam")
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["results"][0]["documents"][0]["source_url"])

    def test_other_cast_and_story(self):
        result = search(self.con, "Mohanlal and Jagathy acted in 1991, tourist guide in Ooty")
        self.assertEqual(result["results"][0]["title"], "Kilukkam")

    def test_role_is_a_graph_constraint(self):
        result = search(self.con, "Rajasenan directed a film in 1991 with a palm reading")
        self.assertEqual([r["title"] for r in result["results"]], ["Kadinjool Kalyanam"])

    def test_wrong_year_rejects_match(self):
        self.assertEqual(search(self.con, "Jayaram and Jagathy acted in 1992")["total"], 0)

    def test_reimport_updates_fts_without_duplicates(self):
        import_json(self.con, EXAMPLES)
        result = search(self.con, "tourist guide Ooty")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["title"], "Kilukkam")
        self.assertEqual(self.con.execute("SELECT count(*) FROM documents").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
