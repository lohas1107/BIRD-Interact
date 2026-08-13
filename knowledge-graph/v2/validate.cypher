// 1. Import cardinalities by label.
MATCH (n) UNWIND labels(n) AS label
RETURN label, count(*) AS node_count ORDER BY label;

// 2. Relationship cardinalities.
MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS edge_count
ORDER BY relationship;

// 3. IDs must be present and unique (the second result must be zero rows).
MATCH (n:KGNode) RETURN count(n) AS total_nodes, count(n.id) AS nodes_with_id;
MATCH (n:KGNode) WITH n.id AS id, count(*) AS copies
WHERE id IS NULL OR copies <> 1 RETURN id, copies;

// 4. Every KB assertion must have a source and a subject.
MATCH (a:Assertion)
WHERE NOT (a)-[:SUPPORTED_BY]->(:ProvenanceSource)
   OR NOT (a)-[:ASSERTS_ABOUT]->(:KGNode)
RETURN a.id AS invalid_assertion;

// 5. Every structured expression retains a raw representation.
MATCH (e:Expression) WHERE e.raw_expression IS NULL
RETURN e.id AS expression_without_raw_source;

// 6. Join conditions must connect one source and one target schema item.
MATCH (j:JoinCondition)
WHERE NOT (:PhysicalSchemaItem)-[:JOINABLE_VIA]->(j)
   OR NOT (j)-[:JOINS_TO]->(:PhysicalSchemaItem)
RETURN j.id AS disconnected_join;

// 7. Dataset/version provenance should return one row.
MATCH (d:Dataset)-[:HAS_VERSION]->(v:Version)
RETURN d.id AS dataset_id, v.kg_version AS kg_version, v.id AS version_id;

// 8. Snapshot regression counts. Every row should report status = "ok".
UNWIND [
  {label:'Domain', expected:18},
  {label:'PhysicalTable', expected:175},
  {label:'PhysicalColumn', expected:2286},
  {label:'Metric', expected:432},
  {label:'Rule', expected:463},
  {label:'JoinCondition', expected:257}
] AS check
CALL {
  WITH check
  MATCH (n:KGNode) WHERE check.label IN labels(n)
  RETURN count(n) AS actual
}
RETURN check.label AS label, check.expected AS expected, actual,
       CASE WHEN actual = check.expected THEN 'ok' ELSE 'mismatch' END AS status;

// 9. No provenance source may lack immutable file identity.
MATCH (s:ProvenanceSource)
WHERE s.path IS NULL OR s.sha256 IS NULL OR size(s.sha256) <> 64
RETURN s.id AS invalid_provenance_source;
