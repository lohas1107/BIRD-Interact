// DANGER: destructive full-database reset for isolated KG evaluation only.
// This deletes every node and relationship in the currently selected database.
MATCH (n) DETACH DELETE n;

// Remove schema objects created by this mapping. These statements are safe
// after the data reset and make the next run start from a known state.
DROP CONSTRAINT v2_kg_node_id IF EXISTS;
DROP INDEX v2_concept_name IF EXISTS;
DROP INDEX v2_knowledge_name IF EXISTS;
DROP INDEX v2_physical_lookup IF EXISTS;
