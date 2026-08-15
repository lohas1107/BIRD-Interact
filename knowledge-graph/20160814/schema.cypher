// kg-v1 graph-owned constraints and lookup index.
// Run after reset.cypher and before mapping.cypher.

CREATE CONSTRAINT kg_v1_database_entity_key IF NOT EXISTS
FOR (n:Database) REQUIRE n.entity_key IS UNIQUE;

CREATE CONSTRAINT kg_v1_table_entity_key IF NOT EXISTS
FOR (n:Table) REQUIRE n.entity_key IS UNIQUE;

CREATE CONSTRAINT kg_v1_column_entity_key IF NOT EXISTS
FOR (n:Column) REQUIRE n.entity_key IS UNIQUE;

CREATE CONSTRAINT kg_v1_foreign_key_entity_key IF NOT EXISTS
FOR (n:ForeignKey) REQUIRE n.entity_key IS UNIQUE;

CREATE CONSTRAINT kg_v1_knowledge_id IF NOT EXISTS
FOR (n:Knowledge) REQUIRE n.id IS UNIQUE;

CREATE INDEX kg_v1_table_lookup IF NOT EXISTS
FOR (n:Table) ON (n.database_name, n.table_name);
