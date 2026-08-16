// Semantic graph constraints and lookup indexes.
// Run after reset.cypher and before mapping.cypher.

CREATE CONSTRAINT kg_v1_database_id IF NOT EXISTS
FOR (n:Database) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT kg_v1_table_id IF NOT EXISTS
FOR (n:Table) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT kg_v1_column_id IF NOT EXISTS
FOR (n:Column) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT kg_v1_foreign_key_id IF NOT EXISTS
FOR (n:ForeignKey) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT kg_v1_knowledge_id IF NOT EXISTS
FOR (n:Knowledge) REQUIRE n.id IS UNIQUE;

CREATE INDEX kg_v1_table_lookup IF NOT EXISTS
FOR (n:Table) ON (n.database_name, n.table_name);

CREATE FULLTEXT INDEX semantic_search_text IF NOT EXISTS
FOR (n:SemanticSearch)
ON EACH [n.search_text];

CREATE VECTOR INDEX semantic_search_embedding IF NOT EXISTS
FOR (n:SemanticSearch)
ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 1536,
  `vector.similarity_function`: 'cosine'
}};
