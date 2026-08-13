// A valid clean import returns no rows from the first two checks and six label counts.
MATCH (n)
WHERE n.kg_version IS NULL OR n.dataset_id IS NULL
   OR n.kg_version <> 'v1' OR n.dataset_id <> 'bird-interact-lite'
RETURN 'wrong_provenance' AS check, count(n) AS failures;

MATCH (n)
WHERE (n:Concept OR n:LogicalEntity OR n:LogicalAttribute OR n:Metric OR n:Rule OR n:PhysicalSchemaItem)
  AND n.id IS NULL
RETURN 'missing_id' AS check, count(n) AS failures;

MATCH (n:Concept) RETURN 'Concept' AS label, count(n) AS count
UNION ALL MATCH (n:LogicalEntity) RETURN 'LogicalEntity', count(n)
UNION ALL MATCH (n:LogicalAttribute) RETURN 'LogicalAttribute', count(n)
UNION ALL MATCH (n:Metric) RETURN 'Metric', count(n)
UNION ALL MATCH (n:Rule) RETURN 'Rule', count(n)
UNION ALL MATCH (n:PhysicalSchemaItem) RETURN 'PhysicalSchemaItem', count(n);

// Snapshot regression check. Every row should report failures=0.
CALL () {
  MATCH (n:Concept) RETURN 'Concept' AS label, count(n) AS actual, 675 AS expected
  UNION ALL MATCH (n:LogicalEntity) RETURN 'LogicalEntity', count(n), 175
  UNION ALL MATCH (n:LogicalAttribute) RETURN 'LogicalAttribute', count(n), 2286
  UNION ALL MATCH (n:Metric) RETURN 'Metric', count(n), 432
  UNION ALL MATCH (n:Rule) RETURN 'Rule', count(n), 463
  UNION ALL MATCH (n:PhysicalSchemaItem) RETURN 'PhysicalSchemaItem', count(n), 2461
}
RETURN 'expected_' + label AS check, actual, expected,
       CASE WHEN actual = expected THEN 0 ELSE 1 END AS failures;
