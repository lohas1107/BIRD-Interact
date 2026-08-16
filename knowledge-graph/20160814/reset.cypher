// DESTRUCTIVE kg-v1 reset.
// The contract intentionally deletes every user node/relationship in the
// selected Neo4j database, then removes only indexes/constraints owned by this
// graph. Neo4j's built-in token lookup structures are not touched.

MATCH (n)
DETACH DELETE n;

DROP CONSTRAINT kg_v1_database_id IF EXISTS;
DROP CONSTRAINT kg_v1_table_id IF EXISTS;
DROP CONSTRAINT kg_v1_column_id IF EXISTS;
DROP CONSTRAINT kg_v1_foreign_key_id IF EXISTS;
DROP CONSTRAINT kg_v1_knowledge_id IF EXISTS;
DROP CONSTRAINT kg_v1_database_entity_key IF EXISTS;
DROP CONSTRAINT kg_v1_table_entity_key IF EXISTS;
DROP CONSTRAINT kg_v1_column_entity_key IF EXISTS;
DROP CONSTRAINT kg_v1_foreign_key_entity_key IF EXISTS;
DROP INDEX kg_v1_table_lookup IF EXISTS;
DROP INDEX semantic_search_text IF EXISTS;
DROP INDEX semantic_search_embedding IF EXISTS;
