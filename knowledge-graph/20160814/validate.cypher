// Read-only semantic graph validation queries.

MATCH (d:Database)
RETURN count(d) AS databases, 18 AS expected_databases;

MATCH (t:Table)
RETURN count(t) AS tables, 175 AS expected_tables;

MATCH (c:Column)
RETURN count(c) AS columns, 2286 AS expected_columns;

MATCH (fk:ForeignKey)
RETURN count(fk) AS foreign_keys, 257 AS expected_foreign_keys;

MATCH (k:Knowledge)
RETURN count(k) AS knowledge_nodes, 1082 AS expected_knowledge_nodes;

OPTIONAL MATCH (ks:KnowledgeSet)
RETURN count(ks) AS knowledge_sets, 0 AS expected_knowledge_sets;

MATCH ()-[r:HAS_KNOWLEDGE]->(:Knowledge)
RETURN count(r) AS has_knowledge_edges, 1082 AS expected_has_knowledge_edges;

MATCH (:Knowledge)-[r:DEPENDS_ON]->(:Knowledge)
RETURN count(r) AS depends_on_edges, 1143 AS expected_depends_on_edges;

MATCH (:Knowledge)-[r:REFERS_TO_COLUMN]->(:Column)
RETURN count(r) AS refers_to_column_edges, 1224 AS expected_refers_to_column_edges;

OPTIONAL MATCH (:Knowledge)-[r:REFERS_TO_COLUMN]->(:Column)
WHERE size(keys(r)) <> 0
RETURN count(r) AS refers_to_column_edges_with_properties,
       0 AS expected_refers_to_column_edges_with_properties;

OPTIONAL MATCH ()-[r:REQUIRES]->()
RETURN count(r) AS legacy_requires_edges, 0 AS expected_legacy_requires_edges;

OPTIONAL MATCH (parent:Knowledge)-[:DEPENDS_ON]->(child:Knowledge)
WHERE split(parent.id, ":")[0] <> split(child.id, ":")[0]
RETURN count(parent) AS cross_database_depends_on_edges,
       0 AS expected_cross_database_depends_on_edges;

OPTIONAL MATCH (source:Knowledge)-[:REFERS_TO_COLUMN]->(column:Column)
WHERE split(source.id, ":")[0] <> split(column.id, ":")[0]
RETURN count(source) AS cross_database_refers_to_column_edges,
       0 AS expected_cross_database_refers_to_column_edges;

OPTIONAL MATCH (a:Knowledge {id: "alien:10"})-[:DEPENDS_ON {position: 0}]->(b:Knowledge {id: "alien:4"})
RETURN count(a) AS alien_10_depends_on_alien_4,
       1 AS expected_alien_10_depends_on_alien_4;

MATCH (k:Knowledge)
WHERE k.id IN ["fake:74", "fake:77"]
RETURN count(k) AS fake_duplicate_name_ids, 2 AS expected_fake_duplicate_name_ids;

OPTIONAL MATCH p=(k:Knowledge)-[:DEPENDS_ON*1..50]->(k)
RETURN count(p) AS dependency_cycles, 0 AS expected_dependency_cycles;

OPTIONAL MATCH (n)
WHERE any(key IN keys(n) WHERE key IN ["kg_version", "snapshot_id", "entity_key"])
RETURN count(n) AS nodes_with_removed_metadata,
       0 AS expected_nodes_with_removed_metadata;

OPTIONAL MATCH (k:Knowledge)
WHERE any(key IN keys(k) WHERE key IN ["snapshot_id", "database_name", "local_id", "kind", "summary"])
RETURN count(k) AS knowledge_nodes_with_removed_properties, 0 AS expected_removed_properties;

OPTIONAL MATCH (c:Column)
WHERE any(key IN keys(c) WHERE key IN ["data_type"])
RETURN count(c) AS columns_with_old_type_property, 0 AS expected_columns_with_old_type_property;

MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
RETURN count(*) AS has_column_edges, 2286 AS expected_has_column_edges;

MATCH (t:Table)-[:HAS_FOREIGN_KEY]->(fk:ForeignKey)-[:REFERENCES_TABLE]->(:Table)
RETURN count(*) AS complete_foreign_key_edges, 257 AS expected_complete_foreign_key_edges;

MATCH ()-[r:JOINS_TO]->()
RETURN count(r) AS joins_to_edges, 257 AS expected_joins_to_edges;

MATCH (n:Knowledge)
WHERE n:SemanticSearch
RETURN count(n) AS searchable_knowledge_nodes, 1082 AS expected_searchable_knowledge_nodes;

MATCH (n:Table)
WHERE n:SemanticSearch
RETURN count(n) AS searchable_tables, 175 AS expected_searchable_tables;

MATCH (n:Column)
WHERE n:SemanticSearch
RETURN count(n) AS searchable_columns, 2286 AS expected_searchable_columns;

MATCH (n:SemanticSearch)
WHERE n.search_text IS NULL
RETURN count(n) AS searchable_nodes_without_search_text,
       0 AS expected_searchable_nodes_without_search_text;

MATCH (n:SemanticSearch)
WHERE n.embedding IS NULL
RETURN count(n) AS searchable_nodes_without_embedding,
       0 AS expected_searchable_nodes_without_embedding;

MATCH (n:SemanticSearch)
WHERE n.embedding_model IS NULL
RETURN count(n) AS searchable_nodes_without_embedding_model,
       0 AS expected_searchable_nodes_without_embedding_model;

MATCH (n:SemanticSearch)
WHERE n.embedding IS NOT NULL AND size(n.embedding) <> 1024
RETURN count(n) AS invalid_embedding_dimensions, 0 AS expected_invalid_embedding_dimensions;

SHOW CONSTRAINTS;
