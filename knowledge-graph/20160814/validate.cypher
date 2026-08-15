// Read-only kg-v1 validation queries. Every statement should return the
// expected value shown in the result columns or in README.md.

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

MATCH ()-[r]->()
RETURN count(r) AS relationships, 5971 AS expected_relationships;

MATCH ()-[r:HAS_KNOWLEDGE]->(:Knowledge)
RETURN count(r) AS has_knowledge_edges, 1082 AS expected_has_knowledge_edges;

MATCH (:Knowledge)-[r:REQUIRES]->(:Knowledge)
RETURN count(r) AS requires_edges, 1143 AS expected_requires_edges;

OPTIONAL MATCH (parent:Knowledge)-[:REQUIRES]->(child:Knowledge)
WHERE split(parent.id, ":")[0] <> split(child.id, ":")[0]
RETURN count(parent) AS cross_database_requires_edges, 0 AS expected_cross_database_requires_edges;

OPTIONAL MATCH (parent:Knowledge)-[:REQUIRES]->(child:Knowledge)
WHERE NOT (parent.id STARTS WITH split(child.id, ":")[0] + ":")
RETURN count(parent) AS malformed_requires_edges, 0 AS expected_malformed_requires_edges;

OPTIONAL MATCH (a:Knowledge {id: "alien:10"})-[:REQUIRES {position: 0}]->(b:Knowledge {id: "alien:4"})
RETURN count(a) AS alien_10_requires_alien_4, 1 AS expected_alien_10_requires_alien_4;

MATCH (k:Knowledge)
WHERE k.id IN ["fake:74", "fake:77"]
RETURN count(k) AS fake_duplicate_name_ids, 2 AS expected_fake_duplicate_name_ids;

OPTIONAL MATCH p=(k:Knowledge)-[:REQUIRES*1..50]->(k)
RETURN count(p) AS dependency_cycles, 0 AS expected_dependency_cycles;

OPTIONAL MATCH (n)
WHERE any(key IN keys(n) WHERE key IN ["kg_version", "snapshot_id"])
RETURN count(n) AS nodes_with_removed_metadata;

OPTIONAL MATCH (k:Knowledge)
WHERE any(key IN keys(k) WHERE key IN ["snapshot_id", "database_name", "local_id", "kind"])
RETURN count(k) AS knowledge_nodes_with_removed_properties, 0 AS expected_knowledge_nodes_with_removed_properties;

OPTIONAL MATCH (t:Table)
WHERE t.description IS NULL
RETURN count(t) AS tables_without_description_property;

OPTIONAL MATCH (c:Column)
WHERE c.ordinal IS NULL
RETURN count(c) AS columns_without_ordinal;

OPTIONAL MATCH (c:Column)
WHERE any(key IN keys(c) WHERE key IN ["full_name", "categories", "source_key", "match_status", "explanation"])
RETURN count(c) AS columns_with_removed_properties;

MATCH (c:Column)
WHERE c.raw_text IS NULL OR c.description IS NULL
RETURN count(c) AS columns_with_missing_documentation,
       2 AS expected_missing_documentation;

MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
RETURN count(*) AS has_column_edges, 2286 AS expected_has_column_edges;

MATCH (t:Table)-[:HAS_FOREIGN_KEY]->(fk:ForeignKey)-[:REFERENCES_TABLE]->(:Table)
RETURN count(*) AS complete_foreign_key_edges, 257 AS expected_complete_foreign_key_edges;

MATCH ()-[r:JOINS_TO]->()
RETURN count(r) AS joins_to_edges, 257 AS expected_joins_to_edges;

MATCH (from:Table {entity_key: "alien:signals"}), (to:Table {entity_key: "alien:observatories"})
MATCH p=allShortestPaths((from)-[:JOINS_TO*..5]-(to))
RETURN [node IN nodes(p) | node.table_name] AS table_path,
       [edge IN relationships(p) | edge.fk_key] AS fk_path;

MATCH (from:Table {entity_key: "cross_db:auditandcompliance"}), (to:Table {entity_key: "cross_db:riskmanagement"})
MATCH p=allShortestPaths((from)-[:JOINS_TO*..5]-(to))
RETURN [node IN nodes(p) | node.table_name] AS table_path,
       [edge IN relationships(p) | edge.fk_key] AS fk_path;

OPTIONAL MATCH (from:Table {entity_key: "crypto:users"}), (to:Table {entity_key: "crypto:fees"})
OPTIONAL MATCH p=allShortestPaths((from)-[:JOINS_TO*..5]-(to))
RETURN count(p) AS crypto_users_to_fees_paths;

SHOW CONSTRAINTS;
