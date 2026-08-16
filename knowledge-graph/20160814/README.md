# Semantic Graph Layer

This directory contains the reproducible Neo4j mapping and validation assets
for the BIRD-Interact semantic layer.

The import order is:

```text
reset.cypher (isolated instance only)
  -> schema.cypher
  -> mapping.cypher
  -> import_embeddings.py
  -> validate.cypher
```

`reset.cypher` is destructive and must not be run against a shared or existing
database. Use a temporary Neo4j instance or namespace for validation.

For the isolated Compose services, start Neo4j and the embedding service from
`BIRD-Interact-Claude`:

```bash
docker compose --profile kg up -d neo4j embedding
python knowledge-graph/20160814/generate_mapping.py
python knowledge-graph/20160814/import_embeddings.py
```

Run the generated Cypher files against that isolated Neo4j instance in the
order shown above; `import_embeddings.py` assumes `schema.cypher` and
`mapping.cypher` have already been applied.

## Graph model

```text
(:Database)-[:HAS_TABLE]->(:Table)-[:HAS_COLUMN]->(:Column)
                         |
                         +--[:HAS_FOREIGN_KEY]->(:ForeignKey)
                                                   |--[:REFERENCES_TABLE]->(:Table)
                                                   |--[:FROM_COLUMN]->(:Column)
                                                   `--[:TO_COLUMN]->(:Column)

(:Table)-[:JOINS_TO]->(:Table)
(:Database)-[:HAS_KNOWLEDGE]->(:Knowledge)
(:Knowledge)-[:DEPENDS_ON {position}]->(:Knowledge)
(:Knowledge)-[:REFERS_TO_COLUMN]->(:Column)
```

`REFERS_TO_COLUMN` has no relationship properties. It is only a semantic
association; JSONB paths, evidence, match method and confidence are not stored
in the graph.

All graph nodes use `id` as their canonical identity:

| Node | Identity example |
| --- | --- |
| Database | `alien` |
| Table | `alien:signals` |
| Column | `alien:signals:snrratio` |
| ForeignKey | `alien:foreign_key:signals:telescref->telescopes:telescregistry` |
| Knowledge | `alien:0` |

Knowledge, Table and Column also have the `SemanticSearch` secondary label.
Semantic-search nodes contain `search_text`; after embedding import they contain a
1024-dimensional `embedding` and `embedding_model`.

Knowledge stores `name`, `type`, `description`, `definition`, `source_file`,
`source_line` and `source_id`. Column stores `column_name`, `column_type`,
`ordinal`, `nullable`, `default_expression`, `is_primary_key` and the parsed
documentation fields.

## Mapping rules

`generate_mapping.py` reads each database's schema, column meanings and JSONL
knowledge source. It:

- preserves duplicate Knowledge names because source IDs define identity;
- creates `DEPENDS_ON` edges in `children_knowledge` order;
- creates `REFERS_TO_COLUMN` when a normalized column identifier occurs in the
  Knowledge name, description or definition;
- keeps all semantic edges inside the database namespace;
- writes deterministic `search_text` for Knowledge, Table and Column nodes;
- emits a rerunnable `mapping.cypher` artifact.

For the lite baseline the generated mapping contains 18 databases, 175 tables,
2,286 columns, 257 foreign keys, 1,082 Knowledge nodes, 1,143 dependency
edges and 1,224 Knowledge-to-column edges.

## Neo4j indexes

`schema.cypher` creates:

- full-text index `semantic_search_text` on `SemanticSearch.search_text`;
- vector index `semantic_search_embedding` on `SemanticSearch.embedding`;
- vector dimensions `1024` and cosine similarity.

The embedding model is `Qwen/Qwen3-Embedding-0.6B`. The independent embedding
service exposes `POST /embed`:

```json
{
  "texts": ["one document", "another document"]
}
```

It returns `embeddings`, `model` and `dimensions`. `import_embeddings.py`
reads all `SemanticSearch` documents, batches them through this endpoint and
updates the Neo4j nodes. Documents are sent without a query instruction.

For semantic queries the search repository sends Qwen's instruction-aware
format:

```text
<Instruct>: Retrieve relevant knowledge definitions or table columns for resolving an ambiguous SQL request.
<Query>: {query}
```

## `search_semantic_context`

The DB service route is `POST /search/semantic_context`. The Claude-facing tool
does not expose `task_id` or `database_name`; the service obtains the selected
database from task context.

Request:

```json
{
  "queries": [
    "signal quality based on signal to noise ratio and noise floor",
    "atmospheric transparency affecting signal detection"
  ],
  "top_k": 5,
  "resource_types": ["knowledge", "table_schema"]
}
```

`queries` contains 1–8 non-empty strings. `top_k` is 1–20. `resource_types`
contains one or both of `knowledge` and `table_schema`.

Each query is embedded and searched independently against vector and full-text
indexes. Results are merged by resource ID, keeping the highest normalized
score. The final `top_k` limit is applied independently to each resource
group. Full-text scores use per-query min-max normalization; the external
score is the maximum of vector and full-text scores.

Response:

```json
{
  "knowledge": [
    {
      "knowledge_id": "alien:0",
      "database_name": "alien",
      "name": "Signal-to-Noise Quality Indicator (SNQI)",
      "type": "calculation_knowledge",
      "score": 0.94,
      "related_columns": [
        {
          "column_id": "alien:signals:snrratio",
          "table_name": "signals",
          "column_name": "snrratio"
        }
      ]
    }
  ],
  "table_schema": [
    {
      "table_id": "alien:signals",
      "table_name": "signals",
      "column_id": "alien:signals:snrratio",
      "column_name": "snrratio",
      "column_type": "numeric",
      "description": "SNR measured for the signal.",
      "score": 0.93
    }
  ]
}
```

Knowledge search applies the task knowledge mask before vector/full-text
retrieval. Masked Knowledge is never returned. Table-schema search returns only
Column resources and never traverses or returns Knowledge.

## `get_knowledge`

Route: `POST /knowledge/graph`.

Request:

```json
{
  "knowledge_id": "alien:10",
  "include": ["description", "definition", "provenance", "related_columns"],
  "expand": {"depth": 1, "nodes": 20}
}
```

`knowledge_id` must match `<database>:<non-negative integer>`. Include values
are `description`, `definition`, `provenance` and `related_columns`. When
`expand` is present, `depth` is 0–5 and `nodes` is 1–50; both are required.
Omitting `include` returns only identity and distance fields.

The response contains `knowledge_id`, BFS-ordered `nodes`, `edges`,
`truncated` and `warnings`. Node identity is returned as `knowledge_id`, and
dependency edges use `from_knowledge_id`, `to_knowledge_id`, `type: DEPENDS_ON`
and `position`.

Hidden roots and nonexistent roots both return `KNOWLEDGE_NOT_FOUND`. Hidden
dependencies and their edges are omitted, so responses never contain dangling
edges.

## `get_table_schema`

Routes: `POST /table_schema` and `POST /get_table_schema`.

Request:

```json
{
  "database_name": "alien",
  "from_table": "signals",
  "to_table": "observatories",
  "include": ["columns", "descriptions", "constraints", "direct_joins"],
  "hops": 5,
  "paths": 5
}
```

`database_name` must equal the task selected database. `from_table` and
`to_table` use the existing exact, case-insensitive join-path semantics.
`hops` is 1–10 and `paths` is 1–20; both default to 5. `join_paths` appears
only when `to_table` is supplied. This tool does not return Knowledge.

## Claude integration

The new graph profile can be run with:

```bash
python -m orchestrator.runner --mode a-interact --tool-profile kg-v1
```

The `kg-v1` tools and costs are:

| Tool | Cost |
| --- | ---: |
| `search_semantic_context` | 1.0 |
| `get_knowledge` | 0.5 |
| `get_table_schema` | 0.5 |
| `ask_user` | 2.0 |
| `execute_sql` | 1.0 |
| `submit_sql` | 3.0 |

Legacy semantic functions remain in the codebase but are not registered in
`kg-v1`. The ADK runtime is intentionally unchanged.
