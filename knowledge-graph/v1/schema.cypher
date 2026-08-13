// Neo4j 5.x constraints and lookup indexes. Safe to execute repeatedly.
CREATE CONSTRAINT v1_concept_id IF NOT EXISTS FOR (n:Concept) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT v1_logical_entity_id IF NOT EXISTS FOR (n:LogicalEntity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT v1_logical_attribute_id IF NOT EXISTS FOR (n:LogicalAttribute) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT v1_metric_id IF NOT EXISTS FOR (n:Metric) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT v1_rule_id IF NOT EXISTS FOR (n:Rule) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT v1_physical_item_id IF NOT EXISTS FOR (n:PhysicalSchemaItem) REQUIRE n.id IS UNIQUE;
CREATE INDEX v1_concept_name IF NOT EXISTS FOR (n:Concept) ON (n.name);
CREATE INDEX v1_entity_name IF NOT EXISTS FOR (n:LogicalEntity) ON (n.name);
CREATE INDEX v1_attribute_name IF NOT EXISTS FOR (n:LogicalAttribute) ON (n.name);
CREATE INDEX v1_metric_name IF NOT EXISTS FOR (n:Metric) ON (n.name);
CREATE INDEX v1_rule_name IF NOT EXISTS FOR (n:Rule) ON (n.name);
CREATE INDEX v1_physical_lookup IF NOT EXISTS FOR (n:PhysicalSchemaItem) ON (n.database, n.table, n.column);
