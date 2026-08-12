#!/usr/bin/env python3
"""Build and verify a small Neo4j knowledge graph for BIRD-Interact lite.

This script is deliberately independent from the benchmark runner.  It only
reads the schema/knowledge files below ``--data-root`` and talks to Neo4j when
the selected action needs it:

    python scripts/knowledge_graph.py --action build --db credit
    python scripts/knowledge_graph.py --action verify --db credit

Canonical identifiers are stable, database-scoped strings:

    db:<database>
    db:<database>|table:<table>
    db:<database>|table:<table>|column:<column>
    db:<database>|knowledge:<source>:<source-id>

Names are retained in node properties.  Identifier components are normalized
to lower-case ASCII tokens so that source files that differ only in SQL
quoting/case still resolve to the same graph node.

Neo4j is accessed through its HTTP transaction endpoint.  ``--reset-db`` is
explicit and scoped to the selected dataset database; there is no global
``MATCH (n) DETACH DELETE n`` operation in this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "bird-interact-lite"
BATCH_SIZE = 500
KB_TYPES = ("calculation_knowledge", "domain_knowledge", "value_illustration")
RELATION_TYPES = (
    "HAS_TABLE",
    "HAS_COLUMN",
    "HAS_KNOWLEDGE",
    "REFERENCES",
    "DEPENDS_ON",
    "DESCRIBES",
    "USES",
    "MENTIONS",
)


class KnowledgeGraphError(RuntimeError):
    """Raised for malformed input or a Neo4j request failure."""


@dataclass(frozen=True)
class ColumnDef:
    name: str
    data_type: str
    definition: str
    ordinal: int
    is_primary_key: bool = False


@dataclass(frozen=True)
class ForeignKey:
    source_table: str
    source_column: str
    target_table: str
    target_column: str


@dataclass
class TableDef:
    name: str
    columns: list[ColumnDef] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass(frozen=True)
class KnowledgeRecord:
    source_id: str
    name: str
    description: str
    definition: str
    knowledge_type: str
    children: list[str]
    children_raw: str


@dataclass
class GraphModel:
    database: str
    nodes: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    relationships: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    warnings: list[str] = field(default_factory=list)
    expected_fk_count: int = 0
    expected_children_count: int = 0

    @property
    def database_id(self) -> str:
        return database_id(self.database)

    def add_node(self, label: str, properties: Mapping[str, Any]) -> str:
        node_id = str(properties["id"])
        existing = self.nodes[label].get(node_id)
        if existing is not None:
            existing.update(properties)
        else:
            self.nodes[label][node_id] = dict(properties)
        return node_id

    def add_relationship(
        self,
        relationship_type: str,
        from_id: str,
        to_id: str,
        properties: Mapping[str, Any] | None = None,
        relationship_id: str | None = None,
    ) -> None:
        if relationship_type not in RELATION_TYPES:
            raise KnowledgeGraphError(f"Unsupported relationship type: {relationship_type}")
        rel_id = relationship_id or f"{relationship_type}:{from_id}:{to_id}"
        key = (from_id, to_id, rel_id)
        self.relationships[relationship_type][key] = {
            "id": rel_id,
            "from_id": from_id,
            "to_id": to_id,
            "properties": dict(properties or {}),
        }

    def node_rows(self, label: str) -> list[dict[str, Any]]:
        return [
            {"id": node_id, "properties": properties}
            for node_id, properties in self.nodes.get(label, {}).items()
        ]

    def relationship_rows(self, relationship_type: str) -> list[dict[str, Any]]:
        return list(self.relationships.get(relationship_type, {}).values())

    def count_nodes(self) -> Counter[str]:
        return Counter({label: len(rows) for label, rows in self.nodes.items()})

    def count_relationships(self) -> Counter[str]:
        return Counter({kind: len(rows) for kind, rows in self.relationships.items()})


def _token(value: str) -> str:
    """Return a stable, case-insensitive identifier component."""

    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.casefold()).strip("_")
    return normalized or "unnamed"


def database_id(database: str) -> str:
    return f"db:{_token(database)}"


def table_id(database: str, table: str) -> str:
    return f"{database_id(database)}|table:{_token(table)}"


def column_id(database: str, table: str, column: str) -> str:
    return f"{table_id(database, table)}|column:{_token(column)}"


def knowledge_id(database: str, source: str, source_id: str) -> str:
    return f"{database_id(database)}|knowledge:{_token(source)}:{_token(source_id)}"


def _strip_identifier(value: str) -> str:
    value = value.strip().rstrip(";").strip()
    if len(value) >= 2:
        if value[0] == value[-1] and value[0] in {'"', "`"}:
            return value[1:-1].replace(value[0] * 2, value[0])
        if value[0] == "[" and value[-1] == "]":
            return value[1:-1]
    return value


def _qualified_parts(value: str) -> list[str]:
    """Split a possibly schema-qualified identifier outside quote characters."""

    parts: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {'"', "`", "["}:
            quote = "]" if char == "[" else char
        elif char == ".":
            parts.append(value[start:index].strip())
            start = index + 1
        index += 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def clean_identifier(value: str) -> str:
    parts = _qualified_parts(value.strip())
    return _strip_identifier(parts[-1] if parts else value)


def _find_matching_parenthesis(text: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise KnowledgeGraphError("Unclosed parenthesis while parsing CREATE TABLE")


def _split_sql_items(body: str) -> list[str]:
    """Split a CREATE TABLE body on top-level commas."""

    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            items.append(body[start:index].strip())
            start = index + 1
        index += 1
    tail = body[start:].strip()
    if tail:
        items.append(tail)
    return items


def _identifier_list(value: str) -> list[str]:
    return [clean_identifier(item) for item in _split_sql_items(value) if item.strip()]


def _column_definition(item: str) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*(?P<name>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)\s+(?P<rest>.+?)\s*$",
        item,
        flags=re.DOTALL,
    )
    if not match:
        return None
    name = clean_identifier(match.group("name"))
    rest = match.group("rest").rstrip(",").strip()
    data_type = re.split(
        r"\s+(?:(?:NOT\s+)?NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|CHECK|UNIQUE)\b",
        rest,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return name, data_type


def parse_schema(ddl: str) -> list[TableDef]:
    """Parse the lite schema files without requiring a new SQL parser package."""

    tables: list[TableDef] = []
    create_pattern = re.compile(r"\bCREATE\s+TABLE\b", flags=re.IGNORECASE)
    search_at = 0
    while match := create_pattern.search(ddl, search_at):
        open_index = ddl.find("(", match.end())
        if open_index < 0:
            raise KnowledgeGraphError("CREATE TABLE has no opening parenthesis")
        header = ddl[match.end() : open_index].strip()
        header = re.sub(r"^IF\s+NOT\s+EXISTS\s+", "", header, flags=re.IGNORECASE).strip()
        table_name = clean_identifier(header)
        close_index = _find_matching_parenthesis(ddl, open_index)
        body = ddl[open_index + 1 : close_index]

        table = TableDef(name=table_name)
        primary_keys: list[str] = []
        ordinal = 0
        for raw_item in _split_sql_items(body):
            item = re.sub(r"--.*?$", "", raw_item, flags=re.MULTILINE).strip()
            if not item:
                continue

            primary_match = re.search(r"\bPRIMARY\s+KEY\s*\(([^)]*)\)", item, re.IGNORECASE)
            if primary_match:
                primary_keys.extend(_identifier_list(primary_match.group(1)))

            foreign_matches = re.finditer(
                r"\bFOREIGN\s+KEY\s*\((?P<src>[^)]*)\)\s*"
                r"REFERENCES\s+(?P<table>.*?)\s*\((?P<dst>[^)]*)\)",
                item,
                flags=re.IGNORECASE | re.DOTALL,
            )
            for foreign_match in foreign_matches:
                sources = _identifier_list(foreign_match.group("src"))
                targets = _identifier_list(foreign_match.group("dst"))
                if len(sources) != len(targets):
                    raise KnowledgeGraphError(
                        f"Mismatched FOREIGN KEY columns in {table_name}: {item}"
                    )
                target_table = clean_identifier(foreign_match.group("table"))
                table.foreign_keys.extend(
                    ForeignKey(table_name, source, target_table, target)
                    for source, target in zip(sources, targets)
                )

            column = _column_definition(item)
            if column is None or re.match(
                r"^(?:CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|EXCLUDE)\b",
                item,
                flags=re.IGNORECASE,
            ):
                continue
            name, data_type = column
            inline_primary = bool(re.search(r"\bPRIMARY\s+KEY\b", item, re.IGNORECASE))
            if inline_primary:
                primary_keys.append(name)
            table.columns.append(
                ColumnDef(
                    name=name,
                    data_type=data_type,
                    definition=item,
                    ordinal=ordinal,
                    is_primary_key=inline_primary,
                )
            )
            ordinal += 1

            inline_reference = re.search(
                r"\bREFERENCES\s+(?P<table>.*?)\s*\((?P<column>[^)]*)\)",
                item,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if inline_reference:
                targets = _identifier_list(inline_reference.group("column"))
                if len(targets) != 1:
                    raise KnowledgeGraphError(f"Unsupported inline FK in {table_name}: {item}")
                table.foreign_keys.append(
                    ForeignKey(
                        table_name,
                        name,
                        clean_identifier(inline_reference.group("table")),
                        targets[0],
                    )
                )

        table.primary_keys = list(dict.fromkeys(primary_keys))
        if table.primary_keys:
            primary_norms = {_token(key) for key in table.primary_keys}
            table.columns = [
                ColumnDef(
                    name=column.name,
                    data_type=column.data_type,
                    definition=column.definition,
                    ordinal=column.ordinal,
                    is_primary_key=column.is_primary_key or _token(column.name) in primary_norms,
                )
                for column in table.columns
            ]
        tables.append(table)
        search_at = close_index + 1

    if not tables:
        raise KnowledgeGraphError("No CREATE TABLE statements found in schema")
    return tables


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeGraphError(f"Could not read JSON file {path}: {exc}") from exc


def load_column_meanings(path: Path) -> list[tuple[str, str]]:
    data = _load_json(path)
    if not isinstance(data, dict):
        raise KnowledgeGraphError(f"Column meanings must be an object: {path}")
    meanings: list[tuple[str, str]] = []
    for source_key, meaning in data.items():
        if not isinstance(source_key, str):
            raise KnowledgeGraphError(f"Invalid column meaning entry in {path}: {source_key!r}")
        if isinstance(meaning, str):
            meaning_text = meaning
        elif isinstance(meaning, (dict, list)):
            # JSONB columns use a structured meaning with nested
            # ``fields_meaning``.  Keep the complete source payload as JSON
            # text so it remains a Neo4j scalar property without losing the
            # nested explanations.
            meaning_text = json.dumps(meaning, ensure_ascii=False, sort_keys=True)
        else:
            raise KnowledgeGraphError(f"Invalid column meaning entry in {path}: {source_key!r}")
        meanings.append((source_key, meaning_text))
    return meanings


def load_kb(path: Path) -> list[KnowledgeRecord]:
    records: list[KnowledgeRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise KnowledgeGraphError(f"Could not read knowledge file {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KnowledgeGraphError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(raw, dict) or "id" not in raw:
            raise KnowledgeGraphError(f"Knowledge entry lacks id at {path}:{line_number}")

        children_raw_value = raw.get("children_knowledge", -1)
        if children_raw_value in (-1, None):
            children: list[str] = []
        elif isinstance(children_raw_value, list):
            children = [str(child) for child in children_raw_value if child != -1]
        else:
            children = [str(children_raw_value)]

        records.append(
            KnowledgeRecord(
                source_id=str(raw["id"]),
                name=str(raw.get("knowledge", "")),
                description=str(raw.get("description", "")),
                definition=str(raw.get("definition", "")),
                knowledge_type=str(raw.get("type", "unknown")),
                children=children,
                children_raw=json.dumps(children_raw_value, ensure_ascii=False, sort_keys=True),
            )
        )
    return records


def _table_lookup(tables: Iterable[TableDef]) -> dict[str, TableDef]:
    return {_token(table.name): table for table in tables}


def _column_lookup(tables: Iterable[TableDef]) -> dict[tuple[str, str], ColumnDef]:
    return {
        (_token(table.name), _token(column.name)): column
        for table in tables
        for column in table.columns
    }


def _column_candidates(tables: Iterable[TableDef]) -> dict[str, list[tuple[str, ColumnDef]]]:
    candidates: dict[str, list[tuple[str, ColumnDef]]] = defaultdict(list)
    for table in tables:
        for column in table.columns:
            candidates[_token(column.name)].append((table.name, column))
    return candidates


def _normalize_reference_text(text: str) -> str:
    text = text.replace(r"\_", "_")
    text = text.replace("`", "").replace('"', "")
    text = re.sub(r"\\text\s*\{", "", text)
    text = text.replace("{", " ").replace("}", " ")
    return text


def _has_identifier(text: str, identifier: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def find_column_references(
    text: str,
    tables: Iterable[TableDef],
) -> list[tuple[str, str, str]]:
    """Find qualified and unambiguous bare column references in knowledge text."""

    table_list = list(tables)
    normalized_text = _normalize_reference_text(text)
    candidates = _column_candidates(table_list)
    found: dict[tuple[str, str], str] = {}

    for table in table_list:
        for column in table.columns:
            qualified = rf"(?<![A-Za-z0-9_]){re.escape(table.name)}\s*\.\s*"
            qualified += rf"{re.escape(column.name)}(?![A-Za-z0-9_])"
            if re.search(qualified, normalized_text, flags=re.IGNORECASE):
                found[(_token(table.name), _token(column.name))] = "qualified"

    for column_key, column_candidates in candidates.items():
        # A bare name is only safe when it identifies one physical column.
        # Qualified references above still work for repeated names.
        if len(column_candidates) != 1 or len(column_key) < 3:
            continue
        table_name, column = column_candidates[0]
        if _has_identifier(normalized_text, column.name):
            found[(_token(table_name), column_key)] = "bare"

    references: list[tuple[str, str, str]] = []
    for key, match_kind in found.items():
        if len(key) != 2:
            continue
        table_key, column_key = key
        table = next((table for table in table_list if _token(table.name) == table_key), None)
        if table is None:
            continue
        column = next((column for column in table.columns if _token(column.name) == column_key), None)
        if column is None:
            continue
        references.append((table.name, column.name, match_kind))
    return sorted(set(references), key=lambda item: (_token(item[0]), _token(item[1])))


def _knowledge_mentions(text: str, target_name: str) -> bool:
    if not target_name.strip():
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(target_name.strip())}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _knowledge_properties(
    database: str,
    source: str,
    source_id: str,
    name: str,
    description: str,
    definition: str,
    knowledge_type: str,
    children: Sequence[str],
    children_raw: str,
) -> dict[str, Any]:
    node_id = knowledge_id(database, source, source_id)
    return {
        "id": node_id,
        "canonical_id": node_id,
        "database_id": database_id(database),
        "database": database,
        "source": source,
        "source_id": source_id,
        "name": name,
        "knowledge": name,
        "description": description,
        "definition": definition,
        "type": knowledge_type,
        "children_knowledge": list(children),
        "children_knowledge_raw": children_raw,
    }


def build_graph_model(database_dir: Path, database: str) -> GraphModel:
    schema_path = database_dir / f"{database}_schema.txt"
    meanings_path = database_dir / f"{database}_column_meaning_base.json"
    kb_path = database_dir / f"{database}_kb.jsonl"
    for required_path in (schema_path, meanings_path, kb_path):
        if not required_path.is_file():
            raise KnowledgeGraphError(f"Missing input file: {required_path}")

    tables = parse_schema(schema_path.read_text(encoding="utf-8"))
    table_by_key = _table_lookup(tables)
    column_by_key = _column_lookup(tables)
    model = GraphModel(database=database)
    db_node_id = model.database_id
    model.add_node(
        "Database",
        {
            "id": db_node_id,
            "canonical_id": db_node_id,
            "database_id": db_node_id,
            "name": database,
            "database": database,
        },
    )

    for table in tables:
        table_node_id = table_id(database, table.name)
        model.add_node(
            "Table",
            {
                "id": table_node_id,
                "canonical_id": table_node_id,
                "database_id": db_node_id,
                "database": database,
                "name": table.name,
                "primary_keys": list(table.primary_keys),
                "column_count": len(table.columns),
            },
        )
        model.add_relationship("HAS_TABLE", db_node_id, table_node_id)
        for column in table.columns:
            column_node_id = column_id(database, table.name, column.name)
            model.add_node(
                "Column",
                {
                    "id": column_node_id,
                    "canonical_id": column_node_id,
                    "database_id": db_node_id,
                    "database": database,
                    "table_id": table_node_id,
                    "table": table.name,
                    "name": column.name,
                    "data_type": column.data_type,
                    "definition": column.definition,
                    "ordinal": column.ordinal,
                    "is_primary_key": column.is_primary_key,
                },
            )
            model.add_relationship("HAS_COLUMN", table_node_id, column_node_id)

    for table in tables:
        for foreign_key in table.foreign_keys:
            model.expected_fk_count += 1
            source_table = table_by_key.get(_token(foreign_key.source_table))
            target_table = table_by_key.get(_token(foreign_key.target_table))
            source_column = column_by_key.get(
                (_token(foreign_key.source_table), _token(foreign_key.source_column))
            )
            target_column = column_by_key.get(
                (_token(foreign_key.target_table), _token(foreign_key.target_column))
            )
            if not all((source_table, target_table, source_column, target_column)):
                model.warnings.append(
                    "Unresolved FK: "
                    f"{foreign_key.source_table}.{foreign_key.source_column} -> "
                    f"{foreign_key.target_table}.{foreign_key.target_column}"
                )
                continue
            source_id = column_id(database, source_table.name, source_column.name)
            target_id = column_id(database, target_table.name, target_column.name)
            relation_id = (
                f"{database_id(database)}|fk:{_token(source_table.name)}.{_token(source_column.name)}"
                f"->{_token(target_table.name)}.{_token(target_column.name)}"
            )
            model.add_relationship(
                "REFERENCES",
                source_id,
                target_id,
                {
                    "database": database,
                    "source_table": source_table.name,
                    "source_column": source_column.name,
                    "target_table": target_table.name,
                    "target_column": target_column.name,
                },
                relationship_id=relation_id,
            )

    kb_records = load_kb(kb_path)
    knowledge_node_ids: dict[tuple[str, str], str] = {}
    for record in kb_records:
        node_id = model.add_node(
            "Knowledge",
            _knowledge_properties(
                database,
                "kb",
                record.source_id,
                record.name,
                record.description,
                record.definition,
                record.knowledge_type,
                record.children,
                record.children_raw,
            ),
        )
        knowledge_node_ids[("kb", record.source_id)] = node_id
        model.add_relationship("HAS_KNOWLEDGE", db_node_id, node_id)
        model.expected_children_count += len(record.children)

    for record in kb_records:
        source_id = knowledge_node_ids[("kb", record.source_id)]
        for child_source_id in record.children:
            child_id = knowledge_node_ids.get(("kb", child_source_id))
            if child_id is None:
                model.warnings.append(
                    f"Unresolved children_knowledge reference: {record.source_id} -> {child_source_id}"
                )
                continue
            model.add_relationship(
                "DEPENDS_ON",
                source_id,
                child_id,
                {"source": "children_knowledge", "child_source_id": child_source_id},
                relationship_id=f"{source_id}|depends:{child_id}",
            )

        text = f"{record.name} {record.description} {record.definition}"
        for referenced_table, referenced_column, match_kind in find_column_references(text, tables):
            referenced_id = column_id(database, referenced_table, referenced_column)
            model.add_relationship(
                "USES",
                source_id,
                referenced_id,
                {"source": "knowledge_text", "match": match_kind},
                relationship_id=f"{source_id}|uses:{referenced_id}",
            )
            model.add_relationship(
                "MENTIONS",
                source_id,
                referenced_id,
                {"source": "knowledge_text", "match": match_kind},
                relationship_id=f"{source_id}|mentions:{referenced_id}",
            )

        for target in kb_records:
            if target.source_id != record.source_id and _knowledge_mentions(text, target.name):
                target_id = knowledge_node_ids[("kb", target.source_id)]
                model.add_relationship(
                    "MENTIONS",
                    source_id,
                    target_id,
                    {"source": "knowledge_text", "target_source_id": target.source_id},
                    relationship_id=f"{source_id}|mentions:{target_id}",
                )

    for source_key, meaning in load_column_meanings(meanings_path):
        parts = source_key.split("|")
        if len(parts) < 3:
            model.warnings.append(f"Unqualified column meaning key: {source_key}")
            source_table_name = ""
            source_column_name = ""
        else:
            source_table_name = parts[-2]
            source_column_name = parts[-1]
        node_id = model.add_node(
            "Knowledge",
            _knowledge_properties(
                database,
                "column_meaning",
                source_key,
                source_key,
                meaning,
                meaning,
                "column_meaning",
                [],
                "-1",
            ),
        )
        model.add_relationship("HAS_KNOWLEDGE", db_node_id, node_id)
        target_column = column_by_key.get(
            (_token(source_table_name), _token(source_column_name))
        )
        target_table = table_by_key.get(_token(source_table_name))
        if target_table is None or target_column is None:
            model.warnings.append(f"Unresolved column meaning target: {source_key}")
            continue
        target_id = column_id(database, target_table.name, target_column.name)
        model.add_relationship(
            "DESCRIBES",
            node_id,
            target_id,
            {"source": "column_meaning", "source_key": source_key},
            relationship_id=f"{node_id}|describes:{target_id}",
        )
        model.add_relationship(
            "MENTIONS",
            node_id,
            target_id,
            {"source": "column_meaning", "source_key": source_key},
            relationship_id=f"{node_id}|mentions:{target_id}",
        )

    return model


def discover_databases(data_root: Path, requested: str) -> list[tuple[str, Path]]:
    if not data_root.is_dir():
        raise KnowledgeGraphError(f"Data root is not a directory: {data_root}")
    available: dict[str, Path] = {}
    for path in sorted(data_root.iterdir()):
        if not path.is_dir():
            continue
        database = path.name
        if (path / f"{database}_schema.txt").is_file():
            available[_token(database)] = path

    if not available:
        raise KnowledgeGraphError(f"No <db>/<db>_schema.txt files found under {data_root}")
    if requested.strip().casefold() in {"", "all", "*"}:
        return [(path.name, path) for path in available.values()]

    names = [name.strip() for name in requested.split(",") if name.strip()]
    selected: list[tuple[str, Path]] = []
    for name in names:
        path = available.get(_token(name))
        if path is None:
            raise KnowledgeGraphError(
                f"Unknown --db {name!r}; available: {', '.join(sorted(path.name for path in available.values()))}"
            )
        selected.append((path.name, path))
    return selected


def _chunks(rows: Sequence[dict[str, Any]], size: int = BATCH_SIZE) -> Iterable[Sequence[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def _import_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise KnowledgeGraphError(
            "httpx is required for Neo4j HTTP actions; it is already listed in requirements.txt"
        ) from exc
    return httpx


def transaction_endpoint(uri: str, neo4j_database: str) -> str:
    endpoint = uri.rstrip("/")
    if endpoint.endswith("/tx/commit"):
        return endpoint
    if endpoint.endswith("/tx"):
        return f"{endpoint}/commit"
    if "/db/" in endpoint:
        return f"{endpoint}/tx/commit"
    return f"{endpoint}/db/{neo4j_database}/tx/commit"


class Neo4jHttpClient:
    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        neo4j_database: str = "neo4j",
        timeout: float = 20.0,
    ) -> None:
        httpx = _import_httpx()
        self.endpoint = transaction_endpoint(uri, neo4j_database)
        self._client = httpx.Client(
            auth=(user, password),
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Neo4jHttpClient":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def run(self, statements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        try:
            response = self._client.post(self.endpoint, json={"statements": list(statements)})
        except Exception as exc:
            raise KnowledgeGraphError(f"Neo4j HTTP request failed at {self.endpoint}: {exc}") from exc
        if response.status_code >= 400:
            raise KnowledgeGraphError(
                f"Neo4j HTTP {response.status_code} at {self.endpoint}: {response.text[:1000]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeGraphError(f"Neo4j returned non-JSON response: {response.text[:1000]}") from exc
        errors = payload.get("errors", [])
        if errors:
            details = "; ".join(
                f"{error.get('code', 'Neo4jError')}: {error.get('message', error)}" for error in errors
            )
            raise KnowledgeGraphError(details)
        return list(payload.get("results", []))


def _statement(statement: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"statement": statement, "parameters": dict(parameters or {})}


def write_graph(client: Neo4jHttpClient, model: GraphModel, reset: bool = False) -> None:
    if reset:
        client.run(
            [
                _statement(
                    "MATCH (n) WHERE n.database_id = $database_id DETACH DELETE n",
                    {"database_id": model.database_id},
                )
            ]
        )

    for label in ("Database", "Table", "Column", "Knowledge"):
        rows = model.node_rows(label)
        query = f"""
UNWIND $rows AS row
MERGE (n:{label} {{id: row.id}})
SET n += row.properties
"""
        for batch in _chunks(rows):
            client.run([_statement(query, {"rows": list(batch)})])

    for relationship_type in RELATION_TYPES:
        rows = model.relationship_rows(relationship_type)
        if not rows:
            continue
        query = f"""
UNWIND $rows AS row
MATCH (a {{id: row.from_id}}), (b {{id: row.to_id}})
MERGE (a)-[r:{relationship_type} {{id: row.id}}]->(b)
SET r += row.properties
"""
        for batch in _chunks(rows):
            client.run([_statement(query, {"rows": list(batch)})])


def _result_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    columns = list(result.get("columns", []))
    rows: list[dict[str, Any]] = []
    for data in result.get("data", []):
        values = list(data.get("row", []))
        rows.append(dict(zip(columns, values)))
    return rows


def _single_value(result: Mapping[str, Any], key: str, default: Any = 0) -> Any:
    rows = _result_rows(result)
    if not rows:
        return default
    return rows[0].get(key, default)


def verify_graph(client: Neo4jHttpClient, model: GraphModel) -> bool:
    statements = [
        _statement(
            """
MATCH (source:Column {database_id: $database_id})
      -[r:REFERENCES]->(target:Column {database_id: $database_id})
RETURN count(r) AS fk_count
""",
            {"database_id": model.database_id},
        ),
        _statement(
            """
MATCH (k:Knowledge {database_id: $database_id})
RETURN k.type AS type, count(k) AS count
ORDER BY type
""",
            {"database_id": model.database_id},
        ),
        _statement(
            """
MATCH (source:Knowledge {database_id: $database_id})
      -[r:DEPENDS_ON]->(target:Knowledge {database_id: $database_id})
RETURN count(r) AS children_count
""",
            {"database_id": model.database_id},
        ),
        _statement(
            """
MATCH (k:Knowledge {database_id: $database_id})
WHERE k.name = $metric_name AND k.type = $metric_type
OPTIONAL MATCH (k)-[:USES]->(column:Column)
WHERE column.name IN $required_columns
RETURN count(DISTINCT k) AS metric_count,
       collect(DISTINCT column.name) AS columns
""",
            {
                "database_id": model.database_id,
                "metric_name": "Net Worth",
                "metric_type": "calculation_knowledge",
                "required_columns": ["totassets", "totliabs"],
            },
        ),
    ]
    results = client.run(statements)
    if len(results) != len(statements):
        raise KnowledgeGraphError(
            f"Neo4j returned {len(results)} results for {len(statements)} verification queries"
        )

    passed = True
    actual_fk_count = int(_single_value(results[0], "fk_count", 0))
    fk_ok = actual_fk_count == model.expected_fk_count
    passed = fk_ok and passed
    print(
        f"[verify] {model.database} FK REFERENCES: "
        f"{actual_fk_count}/{model.expected_fk_count} {'PASS' if fk_ok else 'FAIL'}"
    )

    actual_types = {
        str(row.get("type")): int(row.get("count", 0)) for row in _result_rows(results[1])
    }
    # The check includes column_meaning as an additional graph source while
    # explicitly printing the three benchmark knowledge types.
    expected_all_types = Counter(
        properties.get("type") for properties in model.nodes.get("Knowledge", {}).values()
    )
    type_ok = actual_types == dict(expected_all_types)
    passed = type_ok and passed
    type_parts = []
    for knowledge_type in (*KB_TYPES, "column_meaning"):
        type_parts.append(
            f"{knowledge_type}={actual_types.get(knowledge_type, 0)}"
            f"/{expected_all_types.get(knowledge_type, 0)}"
        )
    print(f"[verify] {model.database} Knowledge types: {', '.join(type_parts)} "
          f"{'PASS' if type_ok else 'FAIL'}")

    actual_children_count = int(_single_value(results[2], "children_count", 0))
    children_ok = actual_children_count == model.expected_children_count
    passed = children_ok and passed
    print(
        f"[verify] {model.database} DEPENDS_ON children refs: "
        f"{actual_children_count}/{model.expected_children_count} "
        f"{'PASS' if children_ok else 'FAIL'}"
    )

    local_has_net_worth = any(
        properties.get("name") == "Net Worth"
        and properties.get("type") == "calculation_knowledge"
        and properties.get("source") == "kb"
        for properties in model.nodes.get("Knowledge", {}).values()
    )
    if local_has_net_worth:
        metric_count = int(_single_value(results[3], "metric_count", 0))
        columns = set(_single_value(results[3], "columns", []) or [])
        required_columns = {"totassets", "totliabs"}
        net_worth_ok = metric_count == 1 and required_columns.issubset(columns)
        passed = net_worth_ok and passed
        print(
            f"[verify] {model.database} Net Worth USES totassets/totliabs: "
            f"metric={metric_count}, columns={sorted(columns)} "
            f"{'PASS' if net_worth_ok else 'FAIL'}"
        )
    else:
        print(f"[verify] {model.database} Net Worth assertion: SKIP (metric not in this database)")

    return passed


def _print_model_summary(action: str, model: GraphModel) -> None:
    node_counts = model.count_nodes()
    relation_counts = model.count_relationships()
    relation_summary = ", ".join(
        f"{kind}={relation_counts.get(kind, 0)}" for kind in RELATION_TYPES if relation_counts.get(kind, 0)
    )
    print(
        f"[{action}] {model.database}: "
        f"tables={node_counts.get('Table', 0)}, "
        f"columns={node_counts.get('Column', 0)}, "
        f"knowledge={node_counts.get('Knowledge', 0)}, "
        f"FK={model.expected_fk_count}; "
        f"relationships: {relation_summary or 'none'}"
    )
    if model.warnings:
        print(f"[{action}] {model.database}: warnings={len(model.warnings)}")
        for warning in model.warnings[:5]:
            print(f"  warning: {warning}")
        if len(model.warnings) > 5:
            print(f"  ... {len(model.warnings) - 5} more warnings")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load and verify the BIRD-Interact lite schema/knowledge graph in Neo4j."
    )
    parser.add_argument("--action", choices=("build", "verify"), required=True)
    parser.add_argument(
        "--db",
        default="all",
        help="Dataset directory name, comma-separated names, or all (default: all).",
    )
    parser.add_argument(
        "--uri",
        default=os.environ.get("NEO4J_URI", "http://127.0.0.1:7474"),
        help="Neo4j HTTP base URI or a complete /db/<name>/tx/commit URI.",
    )
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument(
        "--password",
        default=os.environ.get("NEO4J_PASSWORD", "bird-interact-dev"),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"Directory containing lite DB folders (default: {DEFAULT_DATA_ROOT}).",
    )
    parser.add_argument(
        "--neo4j-database",
        default=os.environ.get("NEO4J_DATABASE", "neo4j"),
        help="Neo4j database name used when --uri is a base URI (default: neo4j).",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="For build only: delete existing graph nodes scoped to each selected dataset DB before loading.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "verify" and args.reset_db:
        parser.error("--reset-db is only valid with --action build")

    try:
        selected = discover_databases(args.data_root, args.db)
        models = [build_graph_model(path, database) for database, path in selected]
        for model in models:
            _print_model_summary(args.action, model)

        with Neo4jHttpClient(
            args.uri,
            args.user,
            args.password,
            neo4j_database=args.neo4j_database,
            timeout=args.timeout,
        ) as client:
            if args.action == "build":
                for model in models:
                    write_graph(client, model, reset=args.reset_db)
                    print(f"[build] {model.database}: Neo4j write complete")
                return 0

            all_passed = True
            for model in models:
                all_passed = verify_graph(client, model) and all_passed
            print(f"[verify] overall: {'PASS' if all_passed else 'FAIL'}")
            return 0 if all_passed else 1
    except KnowledgeGraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
