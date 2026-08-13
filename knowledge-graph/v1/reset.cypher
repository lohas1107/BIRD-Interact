// DESTRUCTIVE FULL RESET: removes every node and relationship in the selected database.
// Intentionally used to make each benchmark run start from an empty Neo4j database.
MATCH (n) DETACH DELETE n;
DROP CONSTRAINT v1_concept_id IF EXISTS;
DROP CONSTRAINT v1_logical_entity_id IF EXISTS;
DROP CONSTRAINT v1_logical_attribute_id IF EXISTS;
DROP CONSTRAINT v1_metric_id IF EXISTS;
DROP CONSTRAINT v1_rule_id IF EXISTS;
DROP CONSTRAINT v1_physical_item_id IF EXISTS;
DROP INDEX v1_concept_name IF EXISTS;
DROP INDEX v1_entity_name IF EXISTS;
DROP INDEX v1_attribute_name IF EXISTS;
DROP INDEX v1_metric_name IF EXISTS;
DROP INDEX v1_rule_name IF EXISTS;
DROP INDEX v1_physical_lookup IF EXISTS;
