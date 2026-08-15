#!/usr/bin/env python3
"""Generate the kg-v1 physical-schema graph mapping.

The generated mapping is source-derived and rerunnable.  The first import
requires ``schema.cypher``; ``reset.cypher`` remains available for a full graph
refresh before ``schema.cypher`` -> ``mapping.cypher`` -> ``validate.cypher``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+"?([^"\s(]+)"?\s*\((.*?)\n\);',
    re.IGNORECASE | re.DOTALL,
)
FOREIGN_KEY_RE = re.compile(
    r"FOREIGN\s+KEY\s*\((?P<from>[^)]*)\)\s*"
    r"REFERENCES\s+\"?(?P<table>[^\"\s(]+)\"?\s*"
    r"\((?P<to>[^)]*)\)",
    re.IGNORECASE,
)
PRIMARY_KEY_RE = re.compile(r"PRIMARY\s+KEY\s*\((?P<columns>[^)]*)\)", re.IGNORECASE)
COLUMN_RE = re.compile(r'^"?(?P<name>[^"\s]+)"?\s+(?P<tail>.+)$')
DEFAULT_RE = re.compile(
    r"\bDEFAULT\s+(?P<value>.+?)(?=\s+(?:NOT\s+NULL|NULL|PRIMARY\s+KEY|REFERENCES|CHECK)\b|$)",
    re.IGNORECASE,
)
TYPE_STOP_RE = re.compile(
    r"\s+(?=(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|CHECK)\b)",
    re.IGNORECASE,
)
EXPLANATION_RE = re.compile(
    r"Explanation:\s*(?P<value>.*?)(?=\s*(?:Data\s+type:|Possible\s+categories:|Example:)|$)",
    re.IGNORECASE | re.DOTALL,
)
DECLARED_TYPE_RE = re.compile(
    r"\b(?:DOUBLE\s+PRECISION|TIMESTAMP(?:\s+(?:WITH|WITHOUT)\s+TIME\s+ZONE)?|"
    r"CHARACTER\s+VARYING|BIGSERIAL|SERIAL|BIGINT|INTEGER|SMALLINT|NUMERIC|"
    r"DECIMAL|REAL|FLOAT|BOOLEAN|BOOL|UUID|JSONB|TEXT|VARCHAR|CHAR|DATE)"
    r"(?:\s*\([^)]*\))?",
    re.IGNORECASE,
)
QUOTED_EXAMPLE_RE = re.compile(
    r"(?:Example|e\.g\.)\s*[:.,]?\s*['\"]([^'\"]+)['\"]", re.IGNORECASE
)
PAREN_EXAMPLE_RE = re.compile(r"\(\s*e\.g\.\s*[,.:]?\s*([^)]*?)\s*\)", re.IGNORECASE)


def compact(value: str) -> str:
    """Compare source display names such as ``ArtifactsCore`` safely."""

    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def cypher(value):
    """Encode a Python scalar/list as a Cypher literal."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(cypher(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def property_map(values: dict) -> str:
    return "{" + ", ".join(
        f"{key}: {cypher(value)}" for key, value in values.items() if value is not None
    ) + "}"


def split_identifiers(raw: str) -> list[str]:
    return [part.strip().strip('"').casefold() for part in raw.split(",") if part.strip()]


def parse_schema(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    tables: list[dict] = []
    foreign_keys: list[dict] = []

    for match in CREATE_TABLE_RE.finditer(text):
        table_name = match.group(1).casefold()
        body = match.group(2)
        primary_keys = set()
        primary_match = PRIMARY_KEY_RE.search(body)
        if primary_match:
            primary_keys.update(split_identifiers(primary_match.group("columns")))

        columns: list[dict] = []
        for ordinal, raw_line in enumerate(body.splitlines(), start=1):
            line = raw_line.strip().rstrip(",")
            if not line or re.match(r"(?:PRIMARY|FOREIGN|UNIQUE|CONSTRAINT|CHECK)\b", line, re.I):
                continue
            column_match = COLUMN_RE.match(line)
            if not column_match:
                continue
            column_name = column_match.group("name").strip('"').casefold()
            tail = column_match.group("tail").strip()
            type_match = TYPE_STOP_RE.search(tail)
            data_type = tail[: type_match.start()].strip() if type_match else tail
            default_match = DEFAULT_RE.search(tail)
            default_expression = default_match.group("value").strip() if default_match else None
            is_primary_key = column_name in primary_keys
            nullable = not bool(re.search(r"\bNOT\s+NULL\b", tail, re.I)) and not is_primary_key
            columns.append({
                "name": column_name,
                "ordinal": len(columns) + 1,
                "data_type": data_type,
                "nullable": nullable,
                "default_expression": default_expression,
                "is_primary_key": is_primary_key,
            })

        tables.append({
            "name": table_name,
            "columns": columns,
            "primary_keys": sorted(primary_keys),
        })

        for fk_match in FOREIGN_KEY_RE.finditer(body):
            foreign_keys.append({
                "from_table": table_name,
                "from_columns": split_identifiers(fk_match.group("from")),
                "to_table": fk_match.group("table").strip('"').casefold(),
                "to_columns": split_identifiers(fk_match.group("to")),
            })

    table_by_name = {table["name"]: table for table in tables}
    seen_keys: set[str] = set()
    for foreign_key in foreign_keys:
        source = table_by_name.get(foreign_key["from_table"], {})
        target = table_by_name.get(foreign_key["to_table"], {})
        source_columns = {column["name"]: column for column in source.get("columns", [])}
        from_is_primary = set(foreign_key["from_columns"]) == set(source.get("primary_keys", []))
        to_is_primary = set(foreign_key["to_columns"]) == set(target.get("primary_keys", []))
        foreign_key["nullable"] = any(
            source_columns.get(name, {}).get("nullable", True)
            for name in foreign_key["from_columns"]
        )
        foreign_key["cardinality"] = "one_to_one" if from_is_primary and to_is_primary else "many_to_one"
        foreign_key["join_condition"] = " AND ".join(
            f"{foreign_key['from_table']}.{left} = {foreign_key['to_table']}.{right}"
            for left, right in zip(foreign_key["from_columns"], foreign_key["to_columns"])
        )
        base_key = (
            f"{foreign_key['from_table']}:{','.join(foreign_key['from_columns'])}"
            f"->{foreign_key['to_table']}:{','.join(foreign_key['to_columns'])}"
        )
        fk_key = base_key
        suffix = 2
        while fk_key in seen_keys:
            fk_key = f"{base_key}#{suffix}"
            suffix += 1
        seen_keys.add(fk_key)
        foreign_key["fk_key"] = fk_key

    return tables, foreign_keys


def flatten_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result = []
        for nested in value.values():
            result.extend(flatten_strings(nested))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(flatten_strings(nested))
        return result
    return []


def extract_examples(strings: list[str]) -> list[str]:
    examples: list[str] = []
    seen: set[str] = set()
    for text in strings:
        for match in QUOTED_EXAMPLE_RE.finditer(text):
            candidate = match.group(1).strip()
            if candidate and candidate not in seen:
                examples.append(candidate)
                seen.add(candidate)
        for match in PAREN_EXAMPLE_RE.finditer(text):
            candidate = match.group(1).strip()
            quoted_candidates = [item for pair in re.findall(r"'([^']+)'|\"([^\"]+)\"", candidate) for item in pair if item]
            candidates = quoted_candidates or [candidate.strip("'\"")]
            for item in candidates:
                item = item.strip()
                if item and item not in seen:
                    examples.append(item)
                    seen.add(item)
    return examples


def parse_meaning(value) -> dict:
    raw_text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    strings = flatten_strings(value)
    primary_text = value if isinstance(value, str) else (
        value.get("column_meaning", raw_text) if isinstance(value, dict) else raw_text
    )
    primary_text = str(primary_text).strip()
    explanation_match = EXPLANATION_RE.search(primary_text)
    description = (
        explanation_match.group("value").strip()
        if explanation_match
        else primary_text
    )
    declared_match = re.search(r"Data\s+type:\s*([^\.]+)", primary_text, re.I)
    if declared_match:
        declared_type = declared_match.group(1).strip()
    else:
        declared_match = DECLARED_TYPE_RE.search(primary_text)
        declared_type = declared_match.group(0).strip() if declared_match else None
        if declared_type is None and re.search(r"\benum\b", primary_text, re.I):
            declared_type = "enum"
    return {
        "raw_text": raw_text,
        "description": description,
        "declared_type": declared_type,
        "examples": extract_examples(strings),
    }


def load_meanings(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], dict] = {}
    for key, value in raw.items():
        parts = str(key).split("|")
        if len(parts) < 3:
            continue
        table = parts[1]
        column = "|".join(parts[2:])
        result.setdefault((compact(table), compact(column)), parse_meaning(value))
    return result


def parse_knowledge(path: Path, database_name: str) -> list[dict]:
    """Read one database's JSONL knowledge source with physical line numbers.

    Knowledge identifiers are scoped by database.  Keeping the source ID as an
    integer while constructing the global string ID here prevents accidental
    name-based deduplication (the source intentionally contains duplicate
    display names in some databases).
    """

    entries: list[dict] = []
    by_source_id: dict[int, dict] = {}
    database_name = database_name.casefold()

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read knowledge source {path}: {exc}") from exc

    with handle:
        for source_line, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                source = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{source_line}: {exc}") from exc
            if not isinstance(source, dict):
                raise ValueError(f"knowledge entry at {path}:{source_line} must be an object")

            source_id = source.get("id")
            if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 0:
                raise ValueError(
                    f"knowledge entry at {path}:{source_line} has invalid non-negative integer id"
                )
            if source_id in by_source_id:
                previous = by_source_id[source_id]["source_line"]
                raise ValueError(
                    f"duplicate knowledge id {database_name}:{source_id} at "
                    f"{path}:{source_line} (previously at line {previous})"
                )

            name = source.get("knowledge")
            source_type = source.get("type")
            if not isinstance(name, str) or not name:
                raise ValueError(f"knowledge entry at {path}:{source_line} has no string knowledge name")
            if not isinstance(source_type, str) or not source_type:
                raise ValueError(f"knowledge entry at {path}:{source_line} has no string type")

            children = source.get("children_knowledge", -1)
            if children == -1:
                children = []
            if (
                not isinstance(children, list)
                or any(
                    isinstance(child_id, bool)
                    or not isinstance(child_id, int)
                    or child_id < 0
                    for child_id in children
                )
            ):
                raise ValueError(
                    f"knowledge entry at {path}:{source_line} has invalid children_knowledge"
                )

            entry = {
                "id": f"{database_name}:{source_id}",
                "source_id": source_id,
                "name": name,
                "type": source_type,
                "summary": source.get("description"),
                "definition": source.get("definition"),
                "source_file": path.name,
                "source_line": source_line,
                "children": list(children),
            }
            by_source_id[source_id] = entry
            entries.append(entry)

    known_ids = set(by_source_id)
    for entry in entries:
        missing = [child_id for child_id in entry["children"] if child_id not in known_ids]
        if missing:
            raise ValueError(
                f"knowledge {entry['id']} references missing child id(s): "
                + ", ".join(str(child_id) for child_id in missing)
            )

    # A dependency cycle would make depth-limited expansion ambiguous.  Check
    # the source before emitting Cypher so malformed imports fail early.
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(source_id: int) -> None:
        if source_id in visiting:
            raise ValueError(f"cycle detected in knowledge dependencies at {database_name}:{source_id}")
        if source_id in visited:
            return
        visiting.add(source_id)
        for child_id in by_source_id[source_id]["children"]:
            visit(child_id)
        visiting.remove(source_id)
        visited.add(source_id)

    for source_id in by_source_id:
        visit(source_id)
    return entries


def emit(source_root: Path, output: Path) -> dict[str, int]:
    stats = {
        "databases": 0,
        "tables": 0,
        "columns": 0,
        "foreign_keys": 0,
        "descriptions": 0,
        "missing_descriptions": 0,
        "knowledge_nodes": 0,
        "requires_edges": 0,
    }
    lines = [
        "// kg-v1 source mapping; generated by generate_mapping.py.",
        "// Run schema.cypher before the first import; this mapping is rerunnable.",
        "// Physical-schema nodes are merged, and Knowledge nodes are source-reconciled.",
        "",
    ]

    parsed_databases: list[
        tuple[str, list[dict], list[dict], dict[tuple[str, str], dict], list[dict]]
    ] = []
    for db_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
        schema_path = db_dir / f"{db_dir.name}_schema.txt"
        meanings_path = db_dir / f"{db_dir.name}_column_meaning_base.json"
        knowledge_path = db_dir / f"{db_dir.name}_kb.jsonl"
        if not schema_path.exists():
            continue
        database_name = db_dir.name.casefold()
        tables, foreign_keys = parse_schema(schema_path)
        meanings = load_meanings(meanings_path)
        knowledge = parse_knowledge(knowledge_path, database_name) if knowledge_path.exists() else []
        parsed_databases.append((database_name, tables, foreign_keys, meanings, knowledge))
        stats["databases"] += 1
        stats["tables"] += len(tables)
        stats["columns"] += sum(len(table["columns"]) for table in tables)
        stats["foreign_keys"] += len(foreign_keys)
        stats["knowledge_nodes"] += len(knowledge)
        stats["requires_edges"] += sum(len(entry["children"]) for entry in knowledge)

    for database_name, tables, foreign_keys, meanings, _ in parsed_databases:
        database_key = database_name
        lines.append(
            f"MERGE (n:Database {{entity_key: {cypher(database_key)}}}) "
            f"SET n = {property_map({'database_name': database_name, 'entity_key': database_key})};"
        )
        for table in tables:
            table_key = f"{database_name}:{table['name']}"
            lines.append(
                f"MERGE (n:Table {{entity_key: {cypher(table_key)}}}) "
                f"SET n = {property_map({'database_name': database_name, 'table_name': table['name'], 'description': '', 'entity_key': table_key})};"
            )
            for column in table["columns"]:
                column_key = f"{table_key}:{column['name']}"
                documented = meanings.get((compact(table["name"]), compact(column["name"])))
                properties = {
                    "database_name": database_name,
                    "table_name": table["name"],
                    "column_name": column["name"],
                    "ordinal": column["ordinal"],
                    "data_type": column["data_type"],
                    "nullable": column["nullable"],
                    "default_expression": column["default_expression"],
                    "is_primary_key": column["is_primary_key"],
                    "entity_key": column_key,
                    "raw_text": documented["raw_text"] if documented else None,
                    "description": documented["description"] if documented else None,
                    "declared_type": documented["declared_type"] if documented else None,
                    "examples": documented["examples"] if documented else [],
                }
                lines.append(
                    f"MERGE (n:Column {{entity_key: {cypher(column_key)}}}) "
                    f"SET n = {property_map(properties)};"
                )
                if documented:
                    stats["descriptions"] += 1
                else:
                    stats["missing_descriptions"] += 1

        for foreign_key in foreign_keys:
            fk_key = f"{database_name}:{foreign_key['fk_key']}"
            entity_key = f"{database_name}:foreign_key:{foreign_key['fk_key']}"
            properties = {
                "fk_key": fk_key,
                "from_columns": foreign_key["from_columns"],
                "to_columns": foreign_key["to_columns"],
                "cardinality": foreign_key["cardinality"],
                "nullable": foreign_key["nullable"],
                "entity_key": entity_key,
            }
            lines.append(
                f"MERGE (n:ForeignKey {{entity_key: {cypher(entity_key)}}}) "
                f"SET n = {property_map(properties)};"
            )

    lines.append("")
    lines.append("// Source-reconciled Knowledge nodes and relationships.")
    for database_name, _, _, _, knowledge in parsed_databases:
        knowledge_ids = [entry["id"] for entry in knowledge]
        # Keep physical schema nodes untouched while removing stale knowledge
        # records and dependency/containment edges for this source namespace.
        lines.append(
            f"MATCH (d:Database {{entity_key: {cypher(database_name)}}})-[r:HAS_KNOWLEDGE]->(:Knowledge) DELETE r;"
        )
        lines.append(
            f"MATCH (parent:Knowledge)-[r:REQUIRES]->() "
            f"WHERE parent.id STARTS WITH {cypher(database_name + ':')} DELETE r;"
        )
        lines.append(
            f"MATCH (stale:Knowledge) WHERE stale.id STARTS WITH {cypher(database_name + ':')} "
            f"AND NOT stale.id IN {cypher(knowledge_ids)} DETACH DELETE stale;"
        )
        for entry in knowledge:
            properties = {
                "id": entry["id"],
                "name": entry["name"],
                "type": entry["type"],
                "summary": entry["summary"],
                "definition": entry["definition"],
                "source_file": entry["source_file"],
                "source_line": entry["source_line"],
                "source_id": entry["source_id"],
            }
            lines.append(
                f"MERGE (n:Knowledge {{id: {cypher(entry['id'])}}}) "
                f"SET n = {property_map(properties)};"
            )

    lines.append("")
    lines.append("// Knowledge containment and ordered dependency edges.")
    for database_name, _, _, _, knowledge in parsed_databases:
        for entry in knowledge:
            lines.append(
                f"MATCH (d:Database {{entity_key: {cypher(database_name)}}}), "
                f"(k:Knowledge {{id: {cypher(entry['id'])}}}) "
                "MERGE (d)-[:HAS_KNOWLEDGE]->(k);"
            )
            for position, child_id in enumerate(entry["children"]):
                child_key = f"{database_name}:{child_id}"
                lines.append(
                    f"MATCH (parent:Knowledge {{id: {cypher(entry['id'])}}}), "
                    f"(child:Knowledge {{id: {cypher(child_key)}}}) "
                    f"MERGE (parent)-[:REQUIRES {{position: {position}}}]->(child);"
                )

    lines.append("")
    lines.append("// Canonical containment and FK edges.")
    for database_name, tables, foreign_keys, _, _ in parsed_databases:
        database_key = database_name
        for table in tables:
            table_key = f"{database_name}:{table['name']}"
            lines.append(
                f"MATCH (d:Database {{entity_key: {cypher(database_key)}}}), (t:Table {{entity_key: {cypher(table_key)}}}) "
                "MERGE (d)-[:HAS_TABLE]->(t);"
            )
            for column in table["columns"]:
                column_key = f"{table_key}:{column['name']}"
                lines.append(
                    f"MATCH (t:Table {{entity_key: {cypher(table_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "MERGE (t)-[:HAS_COLUMN]->(c);"
                )
        for foreign_key in foreign_keys:
            fk_key = f"{database_name}:{foreign_key['fk_key']}"
            fk_entity_key = f"{database_name}:foreign_key:{foreign_key['fk_key']}"
            from_table_key = f"{database_name}:{foreign_key['from_table']}"
            to_table_key = f"{database_name}:{foreign_key['to_table']}"
            lines.append(
                f"MATCH (t:Table {{entity_key: {cypher(from_table_key)}}}), (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}) "
                "MERGE (t)-[:HAS_FOREIGN_KEY]->(fk);"
            )
            lines.append(
                f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (t:Table {{entity_key: {cypher(to_table_key)}}}) "
                "MERGE (fk)-[:REFERENCES_TABLE]->(t);"
            )
            for column_name in foreign_key["from_columns"]:
                column_key = f"{from_table_key}:{column_name}"
                lines.append(
                    f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "MERGE (fk)-[:FROM_COLUMN]->(c);"
                )
            for column_name in foreign_key["to_columns"]:
                column_key = f"{to_table_key}:{column_name}"
                lines.append(
                    f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "MERGE (fk)-[:TO_COLUMN]->(c);"
                )

            join_properties = {
                "fk_key": fk_key,
                "from_columns": foreign_key["from_columns"],
                "to_columns": foreign_key["to_columns"],
                "join_condition": foreign_key["join_condition"],
                "cardinality": foreign_key["cardinality"],
            }
            lines.append(
                f"MATCH (a:Table {{entity_key: {cypher(from_table_key)}}}), (b:Table {{entity_key: {cypher(to_table_key)}}}) "
                f"MERGE (a)-[j:JOINS_TO {{fk_key: {cypher(fk_key)}}}]->(b) "
                f"SET j = {property_map(join_properties)};"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    default_source = Path(__file__).resolve().parents[2] / "BIRD-Interact-Claude" / "bird-interact-lite"
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("mapping.cypher"))
    args = parser.parse_args()
    stats = emit(args.source.resolve(), args.output.resolve())
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
