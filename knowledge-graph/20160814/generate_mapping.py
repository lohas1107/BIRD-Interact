#!/usr/bin/env python3
"""Generate the kg-v1 physical-schema graph mapping.

The generated mapping is intentionally source-derived and non-idempotent.  The
import contract is ``reset.cypher`` -> ``schema.cypher`` -> ``mapping.cypher``
-> ``validate.cypher``; rerunning the mapping without the reset is therefore
an error by design.
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


def emit(source_root: Path, output: Path) -> dict[str, int]:
    stats = {
        "databases": 0,
        "tables": 0,
        "columns": 0,
        "foreign_keys": 0,
        "descriptions": 0,
        "missing_descriptions": 0,
    }
    lines = [
        "// kg-v1 source mapping; generated by generate_mapping.py.",
        "// Import only after reset.cypher and schema.cypher.",
        "// This file intentionally uses CREATE, not MERGE; rerun the reset first.",
        "",
    ]

    parsed_databases: list[tuple[str, list[dict], list[dict], dict[tuple[str, str], dict]]] = []
    for db_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
        schema_path = db_dir / f"{db_dir.name}_schema.txt"
        meanings_path = db_dir / f"{db_dir.name}_column_meaning_base.json"
        if not schema_path.exists():
            continue
        tables, foreign_keys = parse_schema(schema_path)
        meanings = load_meanings(meanings_path)
        parsed_databases.append((db_dir.name, tables, foreign_keys, meanings))
        stats["databases"] += 1
        stats["tables"] += len(tables)
        stats["columns"] += sum(len(table["columns"]) for table in tables)
        stats["foreign_keys"] += len(foreign_keys)

    for database_name, tables, foreign_keys, meanings in parsed_databases:
        database_key = database_name
        lines.append(
            f"CREATE (n:Database {property_map({'database_name': database_name, 'entity_key': database_key})});"
        )
        for table in tables:
            table_key = f"{database_name}:{table['name']}"
            lines.append(
                f"CREATE (n:Table {property_map({'database_name': database_name, 'table_name': table['name'], 'description': '', 'entity_key': table_key})});"
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
                lines.append(f"CREATE (n:Column {property_map(properties)});")
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
            lines.append(f"CREATE (n:ForeignKey {property_map(properties)});")

    lines.append("")
    lines.append("// Canonical containment and FK edges.")
    for database_name, tables, foreign_keys, _ in parsed_databases:
        database_key = database_name
        for table in tables:
            table_key = f"{database_name}:{table['name']}"
            lines.append(
                f"MATCH (d:Database {{entity_key: {cypher(database_key)}}}), (t:Table {{entity_key: {cypher(table_key)}}}) "
                "CREATE (d)-[:HAS_TABLE]->(t);"
            )
            for column in table["columns"]:
                column_key = f"{table_key}:{column['name']}"
                lines.append(
                    f"MATCH (t:Table {{entity_key: {cypher(table_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "CREATE (t)-[:HAS_COLUMN]->(c);"
                )
        for foreign_key in foreign_keys:
            fk_key = f"{database_name}:{foreign_key['fk_key']}"
            fk_entity_key = f"{database_name}:foreign_key:{foreign_key['fk_key']}"
            from_table_key = f"{database_name}:{foreign_key['from_table']}"
            to_table_key = f"{database_name}:{foreign_key['to_table']}"
            lines.append(
                f"MATCH (t:Table {{entity_key: {cypher(from_table_key)}}}), (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}) "
                "CREATE (t)-[:HAS_FOREIGN_KEY]->(fk);"
            )
            lines.append(
                f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (t:Table {{entity_key: {cypher(to_table_key)}}}) "
                "CREATE (fk)-[:REFERENCES_TABLE]->(t);"
            )
            for column_name in foreign_key["from_columns"]:
                column_key = f"{from_table_key}:{column_name}"
                lines.append(
                    f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "CREATE (fk)-[:FROM_COLUMN]->(c);"
                )
            for column_name in foreign_key["to_columns"]:
                column_key = f"{to_table_key}:{column_name}"
                lines.append(
                    f"MATCH (fk:ForeignKey {{entity_key: {cypher(fk_entity_key)}}}), (c:Column {{entity_key: {cypher(column_key)}}}) "
                    "CREATE (fk)-[:TO_COLUMN]->(c);"
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
                f"CREATE (a)-[:JOINS_TO {property_map(join_properties)}]->(b);"
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
