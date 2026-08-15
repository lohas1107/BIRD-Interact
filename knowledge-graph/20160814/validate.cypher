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

MATCH ()-[r]->()
RETURN count(r) AS relationships, 3746 AS expected_relationships;

MATCH (n)
WHERE any(key IN keys(n) WHERE key IN ["kg_version", "snapshot_id"])
RETURN count(n) AS nodes_with_removed_metadata;

MATCH (t:Table)
WHERE t.description IS NULL
RETURN count(t) AS tables_without_description_property;

MATCH (c:Column)
WHERE c.ordinal IS NULL
RETURN count(c) AS columns_without_ordinal;

MATCH (c:Column)
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

MATCH (from:Table {entity_key: "crypto:users"}), (to:Table {entity_key: "crypto:fees"})
MATCH p=allShortestPaths((from)-[:JOINS_TO*..5]-(to))
RETURN count(p) AS crypto_users_to_fees_paths;

SHOW CONSTRAINTS;
