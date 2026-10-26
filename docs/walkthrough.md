# Walkthrough: “mohanlal and mammokka movies”

Generated 2026-10-25 16:13 by `scripts/explain_query.py` against the live data.

## 1. Normalise + gazetteer NER (longest match, nicknames, fuzzy repair)

`Gazetteer.tag` — `src/filmgraph/nlu.py:202`

Every n-gram is looked up in a dictionary of 38k KG names, aliases, titles, places and curated nicknames.

```json
{
  "normalised": "mohanlal and mammokka movies",
  "spans": [
    {
      "text": "mohanlal",
      "corrected_to": null,
      "candidates": [
        {
          "type": "PERSON",
          "id": "wiki:Krittika_Pradeep",
          "name": "Mohanlal",
          "weight": 1
        },
        {
          "type": "PERSON",
          "id": "wiki:Mohanlal",
          "name": "Mohanlal",
          "weight": 382
        },
        {
          "type": "PERSON",
          "id": "wiki:Seema_(Actress)",
          "name": "Seema",
          "weight": 188
        }
      ]
    },
    {
      "text": "mammokka",
      "corrected_to": "mammookka",
      "candidates": [
        {
          "type": "PERSON",
          "id": "wiki:Mammootty",
          "name": "Mammootty",
          "weight": 10426
        }
      ]
    }
  ]
}
```

## 2. Entity linking: one KG id per mention

`link` — `src/filmgraph/nlu.py:261`

Exact names beat stray aliases; comparable candidates become a clarification.

```json
{
  "entities": [
    {
      "type": "PERSON",
      "id": "wiki:Mohanlal",
      "name": "Mohanlal",
      "text": "mohanlal"
    },
    {
      "type": "PERSON",
      "id": "wiki:Mammootty",
      "name": "Mammootty",
      "text": "mammokka"
    }
  ],
  "ambiguous": []
}
```

## 3. Role and time slots

`assign_roles` — `src/filmgraph/nlu.py:307`

```json
{
  "roles": {
    "Mohanlal": null,
    "Mammootty": null
  },
  "time_slots": {}
}
```

## 4. Delexicalise + intent routing (rules → classifier → LLM)

`understand` — `src/filmgraph/nlu.py:520`

```json
{
  "delexicalised": "PERSON and PERSON movies",
  "rule_fired": null,
  "classifier_top3": [
    [
      "collaboration",
      0.921
    ],
    [
      "filmography",
      0.053
    ],
    [
      "connection_path",
      0.014
    ]
  ],
  "intent": "collaboration",
  "method": "classifier",
  "confidence": 0.921
}
```

## 5. Default roles (people who are actors → films they both ACTED in)

`default_roles` — `src/filmgraph/pipeline.py:164`

```json
{
  "before": [
    {
      "id": "wiki:Mohanlal",
      "name": "Mohanlal",
      "roles": []
    },
    {
      "id": "wiki:Mammootty",
      "name": "Mammootty",
      "roles": []
    }
  ],
  "after": [
    {
      "id": "wiki:Mohanlal",
      "name": "Mohanlal",
      "roles": [
        "actor"
      ]
    },
    {
      "id": "wiki:Mammootty",
      "name": "Mammootty",
      "roles": [
        "actor"
      ]
    }
  ],
  "trace": [
    {
      "stage": "default_roles",
      "ms": 0.0,
      "rule": "all named people are primarily actors -> roles=[actor]",
      "primary_roles": {
        "wiki:Mohanlal": "actor",
        "wiki:Mammootty": "actor"
      }
    }
  ]
}
```

## 6. Parameterised graph query on neo4j

`Neo4jStore.films_with` — `src/filmgraph/graphstore.py:169`

Only validated ids, whitelisted relationship types and integers reach the query; user text never does.

```cypher
MATCH (f:Film)
WHERE (EXISTS { MATCH (:Person {id: $p0})-[:ACTED_IN]->(f) })
  AND (EXISTS { MATCH (:Person {id: $p1})-[:ACTED_IN]->(f) })
RETURN f.id AS id, f.title AS title, f.year AS year ORDER BY coalesce(year, 0) DESC, title LIMIT $limit
```

```json
{
  "params": [
    "wiki:Mohanlal",
    "wiki:Mammootty"
  ],
  "rows": 62,
  "ms": 75.7,
  "first_rows": [
    {
      "id": "wiki:Patriot_(film)",
      "title": "Patriot",
      "year": 2026
    },
    {
      "id": "wiki:Barroz",
      "title": "Barroz",
      "year": 2024
    },
    {
      "id": "wiki:Pathonpatham_Noottandu",
      "title": "Pathonpatham Noottandu",
      "year": 2022
    },
    {
      "id": "wiki:Jack_&_Daniel",
      "title": "Jack & Daniel",
      "year": 2019
    },
    {
      "id": "wiki:Kadal_Kadannu_Oru_Maathukutty",
      "title": "Kadal Kadannu Oru Maathukutty",
      "year": 2013
    }
  ]
}
```

## 7. Provenance per edge: is each credit on the film’s own Wikipedia page?

`evidence` — `src/filmgraph/pipeline.py:149`

```json
{
  "confirmed": 57,
  "weak": 5,
  "weak_examples": [
    {
      "film": "Jack & Daniel (2019)",
      "evidence": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "wikidata"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "wikidata"
          ]
        }
      }
    },
    {
      "film": "Husbands in Goa (2012)",
      "evidence": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "wikidata"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "wikidata"
          ]
        }
      }
    },
    {
      "film": "Pazhassi Raja (2009)",
      "evidence": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb",
            "wikidata",
            "wikipedia"
          ]
        }
      }
    },
    {
      "film": "Sesham Kazhchayil (1983)",
      "evidence": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb",
            "wikidata"
          ]
        }
      }
    },
    {
      "film": "Theeram Thedunna Thira (1983)",
      "evidence": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb",
            "wikipedia"
          ]
        }
      }
    }
  ],
  "confirmed_example": {
    "id": "wiki:Patriot_(film)",
    "title": "Patriot",
    "year": 2026,
    "evidence": {
      "confirmed": true,
      "people": {
        "Mohanlal": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb",
            "wikidata",
            "wikipedia"
          ]
        },
        "Mammootty": {
          "roles": [
            "actor"
          ],
          "sources": [
            "imdb",
            "wikidata",
            "wikipedia"
          ]
        }
      }
    }
  }
}
```

## 8. Full pipeline answer (cards + graph + trace)

`answer` — `src/filmgraph/pipeline.py:41`

**Answer shown in chat:**

> **57 films** credit Mohanlal (actor) and Mammootty (actor) (1980–2026), each confirmed by the film's Wikipedia page. Most recent: Patriot (2026), Barroz (2024), Pathonpatham Noottandu (2022).
> 
> ⚠ **5 more** rest only on Wikidata/IMDb claims and may be cameos, voice-overs or data errors: Jack & Daniel (2019), Husbands in Goa (2012), Pazhassi Raja (2009), Sesham Kazhchayil (1983), Theeram Thedunna Thira (1983).

```json
{
  "flow": "collaboration",
  "backend": "neo4j",
  "total_ms": 47.8,
  "cards": [
    "Patriot (2026) — ✓",
    "Barroz (2024) — ✓",
    "Pathonpatham Noottandu (2022) — ✓",
    "Kadal Kadannu Oru Maathukutty (2013) — ✓",
    "Chattambinadu (ചട്ടമ്പിനാടു്) (2009) — ✓",
    "Twenty:20 (2008) — ✓",
    "Narasimham (2000) — ✓",
    "Harikrishnans (1998) — ✓"
  ],
  "graph": {
    "nodes": 226,
    "edges": 537,
    "dashed_weak_edges": 39
  },
  "trace": [
    {
      "stage": "nlu",
      "ms": 6.2,
      "intent": "collaboration",
      "method": "classifier",
      "confidence": 0.921,
      "slots": {},
      "ner_ms": 5.1,
      "classifier_ms": 1.1,
      "total_ms": 6.2
    },
    {
      "stage": "default_roles",
      "ms": 0.0,
      "rule": "all named people are primarily actors -> roles=[actor]",
      "primary_roles": {
        "wiki:Mohanlal": "actor",
        "wiki:Mammootty": "actor"
      }
    },
    {
      "stage": "graph_query",
      "ms": 28.3,
      "backend": "neo4j",
      "template": "films_with",
      "params": [
        "wiki:Mohanlal",
        "wiki:Mammootty"
      ],
      "rows": 62
    },
    {
      "stage": "provenance",
      "ms": 29.0,
      "confirmed": 57,
      "weak": 5
    },
    {
      "stage": "hydrate",
      "ms": 12.5,
      "cards": 12,
      "nodes": 226
    }
  ]
}
```
