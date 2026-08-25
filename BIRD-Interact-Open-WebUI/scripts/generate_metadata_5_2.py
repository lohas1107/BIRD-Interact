#!/usr/bin/env python3
"""Generate the fixed metadata-5-2 corpus.

This is an offline, one-time build command.  It reads only schema files,
column meanings, the allowed KB prose fields, and the allowed ambiguity
``term``/``type`` fields from the lite task data.  It deliberately does not
read or copy ``external_knowledge``, ``sol_sql``, ``test_cases``,
``sql_snippet``, or ground-truth condition fields.

Observed ranges are the one exception to source-file generation: every range
is obtained through a read-only PostgreSQL connection.  If any database or
column query cannot be completed, no corpus files are written and the command
exits non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import settings
from db_environment.metadata_5_2 import (
    COLUMN_FIELDS,
    DATASET_FIELDS,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    GENERATOR_MODEL,
    METADATA_SCHEMA_VERSION,
    canonical_manifest_hash,
)


CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+"?(?P<name>[^"\s(]+)"?\s*\((?P<body>.*?)\n\);',
    re.IGNORECASE | re.DOTALL,
)
FOREIGN_KEY_RE = re.compile(
    r"FOREIGN\s+KEY\s*\((?P<from>[^)]*)\)\s*"
    r"REFERENCES\s+\"?(?P<table>[^\"\s(]+)\"?\s*\((?P<to>[^)]*)\)",
    re.IGNORECASE,
)
PRIMARY_KEY_RE = re.compile(r"PRIMARY\s+KEY\s*\((?P<columns>[^)]*)\)", re.IGNORECASE)
COLUMN_RE = re.compile(r'^"?(?P<name>[^"\s]+)"?\s+(?P<tail>.+)$')
TYPE_STOP_RE = re.compile(
    r"\s+(?=(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|REFERENCES|CHECK)\b)",
    re.IGNORECASE,
)
IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class MetadataGenerationError(RuntimeError):
    """Raised before any output is written when generation is incomplete."""


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _tokens(value: str) -> set[str]:
    return {_compact(token) for token in IDENTIFIER_RE.findall(value or "")}


def _split_identifiers(value: str) -> list[str]:
    return [part.strip().strip('"').casefold() for part in value.split(",") if part.strip()]


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        # Preserve prose but never serialize source field names.  KB entries
        # are read through their allowed prose values before this function.
        return " ".join(text for text in (_flatten_text(item) for item in value.values()) if text)
    if isinstance(value, list):
        return " ".join(text for text in (_flatten_text(item) for item in value) if text)
    return "" if value is None else str(value)


def parse_schema(path: Path) -> list[dict[str, Any]]:
    """Parse the source schema dump without connecting to PostgreSQL."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MetadataGenerationError(f"cannot read schema {path}: {exc}") from exc

    tables: list[dict[str, Any]] = []
    for match in CREATE_TABLE_RE.finditer(text):
        table_name = match.group("name").casefold()
        body = match.group("body")
        primary_match = PRIMARY_KEY_RE.search(body)
        primary_keys = set(_split_identifiers(primary_match.group("columns"))) if primary_match else set()
        columns: list[dict[str, Any]] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or re.match(r"(?:PRIMARY|FOREIGN|UNIQUE|CONSTRAINT|CHECK)\b", line, re.IGNORECASE):
                continue
            match_column = COLUMN_RE.match(line)
            if not match_column:
                continue
            column_name = match_column.group("name").strip('"').casefold()
            tail = match_column.group("tail").strip()
            type_match = TYPE_STOP_RE.search(tail)
            column_type = tail[: type_match.start()].strip() if type_match else tail
            columns.append({
                "column_name": column_name,
                "column_type": column_type,
                "ordinal": len(columns) + 1,
                "nullable": column_name not in primary_keys and not bool(re.search(r"\bNOT\s+NULL\b", tail, re.IGNORECASE)),
                "is_primary_key": column_name in primary_keys,
            })

        foreign_keys: list[dict[str, Any]] = []
        column_by_name = {column["column_name"]: column for column in columns}
        for foreign_match in FOREIGN_KEY_RE.finditer(body):
            from_columns = _split_identifiers(foreign_match.group("from"))
            to_table = foreign_match.group("table").strip('"').casefold()
            to_columns = _split_identifiers(foreign_match.group("to"))
            from_is_primary = set(from_columns) == primary_keys
            foreign_keys.append({
                "from_columns": from_columns,
                "to_table": to_table,
                "to_columns": to_columns,
                "nullable": any(column_by_name.get(name, {}).get("nullable", True) for name in from_columns),
                "cardinality": "one_to_one" if from_is_primary else "many_to_one",
                "join_condition": " AND ".join(
                    f"{table_name}.{left} = {to_table}.{right}"
                    for left, right in zip(from_columns, to_columns)
                ),
            })
        tables.append({
            "table_name": table_name,
            "columns": columns,
            "primary_keys": sorted(primary_keys),
            "foreign_keys": foreign_keys,
        })
    if not tables:
        raise MetadataGenerationError(f"no CREATE TABLE statements found in {path}")
    return tables


def load_column_meanings(path: Path) -> dict[tuple[str, str], str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataGenerationError(f"cannot read column meanings {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MetadataGenerationError(f"column meanings {path} must be an object")
    result: dict[tuple[str, str], str] = {}
    for raw_key, value in raw.items():
        parts = str(raw_key).split("|")
        if len(parts) < 3:
            continue
        table_name = _compact(parts[1])
        column_name = _compact("|".join(parts[2:]))
        # Keep only the meaning prose; source key names are not metadata.
        if isinstance(value, Mapping):
            text = _flatten_text(value.get("column_meaning", value.get("description", value)))
        else:
            text = _flatten_text(value)
        result.setdefault((table_name, column_name), text)
    return result


def load_knowledge(path: Path, database: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MetadataGenerationError(f"cannot read knowledge source {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            source = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MetadataGenerationError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(source, Mapping):
            raise MetadataGenerationError(f"knowledge entry in {path}:{line_number} must be an object")
        source_id = source.get("id")
        name = source.get("knowledge")
        source_type = source.get("type")
        if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 0:
            raise MetadataGenerationError(f"knowledge entry in {path}:{line_number} has invalid id")
        if not isinstance(name, str) or not name or not isinstance(source_type, str) or not source_type:
            raise MetadataGenerationError(f"knowledge entry in {path}:{line_number} has invalid identity")
        entries.append({
            "id": f"{database}:{source_id}",
            "name": name.strip(),
            "type": source_type.strip(),
            "description": _flatten_text(source.get("description")),
            "definition": _flatten_text(source.get("definition")),
        })
    return entries


def load_ambiguities(path: Path) -> dict[str, list[dict[str, str]]]:
    """Read only ambiguity ``term`` and ``type`` from task records."""
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataGenerationError(f"cannot read task source {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MetadataGenerationError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(task, Mapping):
            continue
        database = task.get("selected_database")
        if not isinstance(database, str) or not database:
            continue
        database = database.casefold()
        for source_key in ("user_query_ambiguity", "knowledge_ambiguity"):
            source = task.get(source_key)
            sections: Iterable[Any]
            if isinstance(source, Mapping):
                sections = source.values()
            elif isinstance(source, list):
                sections = (source,)
            else:
                continue
            for section in sections:
                if isinstance(section, Mapping):
                    section_items = (section,)
                elif isinstance(section, list):
                    section_items = section
                else:
                    continue
                for item in section_items:
                    if not isinstance(item, Mapping):
                        continue
                    # Do not access, copy, or transform sql_snippet or any
                    # other ground-truth field.
                    term = item.get("term")
                    ambiguity_type = item.get("type")
                    if isinstance(term, str) and term.strip() and isinstance(ambiguity_type, str) and ambiguity_type.strip():
                        result[database].append({"term": term.strip(), "type": ambiguity_type.strip()})
    # Stable de-duplication keeps the corpus deterministic.
    for database, items in result.items():
        seen: set[tuple[str, str]] = set()
        result[database] = [
            item
            for item in items
            if not ((key := (item["term"].casefold(), item["type"].casefold())) in seen or seen.add(key))
        ]
    return dict(result)


def _fragment(value: str, refs: Iterable[str] = ()) -> dict[str, Any]:
    return {"value": value.strip(), "knowledge_refs": list(dict.fromkeys(refs))}


def _references_for_text(text: str, knowledge: list[dict[str, str]]) -> list[str]:
    haystack = text.casefold()
    return [
        entry["id"]
        for entry in knowledge
        if entry["name"].casefold() in haystack
        or (entry["description"] and entry["description"].casefold() in haystack)
    ]


def _related_columns(
    tables: list[dict[str, Any]],
    meanings: Mapping[tuple[str, str], str],
    text: str,
) -> set[str]:
    text_tokens = _tokens(text)
    related: set[str] = set()
    for table in tables:
        table_name = table["table_name"]
        table_tokens = _tokens(table_name)
        table_hit = bool(table_tokens & text_tokens)
        for column in table["columns"]:
            column_name = column["column_name"]
            # Exact normalized identifiers are strong evidence.  Broad prose
            # overlap (for example the word "name" in many meanings) would
            # incorrectly copy a whole dataset's formulas onto unrelated
            # columns, so meanings are intentionally not used as a wildcard.
            column_hit = _compact(column_name) in text_tokens
            if table_hit or column_hit:
                related.add(f"{table_name}:{column_name}")
    return related


def _structure(table: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "primary_key": list(table["primary_keys"]),
        "foreign_keys": [
            {
                "from_columns": list(foreign_key["from_columns"]),
                "to_table": foreign_key["to_table"],
                "to_columns": list(foreign_key["to_columns"]),
                "cardinality": foreign_key["cardinality"],
                "nullable": bool(foreign_key["nullable"]),
                "join_condition": foreign_key["join_condition"],
            }
            for foreign_key in table["foreign_keys"]
        ],
    }


def _semantic_fragments(
    database: str,
    tables: list[dict[str, Any]],
    meanings: Mapping[tuple[str, str], str],
    knowledge: list[dict[str, str]],
    ambiguities: list[dict[str, str]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    dataset: dict[str, list[dict[str, Any]]] = {
        "formulas": [],
        "classification_rules": [],
        "disambiguation_rules": [],
        "known_discrepancies": [],
    }
    relevant_by_column: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {
        "formulas": [],
        "classification_rules": [],
        "disambiguation_rules": [],
        "known_discrepancies": [],
        "column_identity_references": [],
    })

    for entry in knowledge:
        source_text = " ".join(item for item in (entry["name"], entry["description"], entry["definition"]) if item)
        if not source_text:
            continue
        ref = [entry["id"]]
        related = _related_columns(tables, meanings, source_text)
        lowered = source_text.casefold()
        is_formula = bool(re.search(r"formula|calculat|comput|metric|factor|ratio|score|rate|index|\s=\s", lowered))
        is_classification = bool(re.search(r"classif|categor|label|threshold|high|medium|low|tier|status|rule", lowered))
        if is_formula:
            value = f"{entry['name']}: {entry['definition'] or entry['description'] or entry['name']}"
            fragment = _fragment(value, ref)
            dataset["formulas"].append(fragment)
            for key in related:
                relevant_by_column[key]["formulas"].append(fragment)
        if is_classification:
            value = f"{entry['name']}: {entry['definition'] or entry['description'] or entry['name']}"
            fragment = _fragment(value, ref)
            dataset["classification_rules"].append(fragment)
            for key in related:
                relevant_by_column[key]["classification_rules"].append(fragment)

    for ambiguity in ambiguities:
        term = ambiguity["term"]
        ambiguity_type = ambiguity["type"]
        value = (
            f"Resolve the term {term!r} as a {ambiguity_type} ambiguity; "
            "confirm the intended meaning before writing SQL."
        )
        fragment = _fragment(value, _references_for_text(term, knowledge))
        dataset["disambiguation_rules"].append(fragment)
        related = _related_columns(tables, meanings, term)
        for key in related:
            relevant_by_column[key]["disambiguation_rules"].append(fragment)

    # Deduplicate fragments while preserving deterministic source order.
    for field, items in dataset.items():
        seen: set[tuple[str, tuple[str, ...]]] = set()
        dataset[field] = [
            item
            for item in items
            if not ((key := (item["value"], tuple(item["knowledge_refs"]))) in seen or seen.add(key))
        ]
    for key, fields in relevant_by_column.items():
        for field, items in fields.items():
            seen = set()
            fields[field] = [
                item
                for item in items
                if not ((dedupe_key := (item["value"], tuple(item["knowledge_refs"]))) in seen or seen.add(dedupe_key))
            ]
    return dataset, dict(relevant_by_column)


class PostgreSQLObservedRangeReader:
    """Read observed min/max values using PostgreSQL read-only transactions."""

    def __init__(self, config=settings):
        self.config = config

    def read(self, database: str, tables: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        try:
            import psycopg2
            from psycopg2 import sql
        except ImportError as exc:  # pragma: no cover - requirements install path
            raise MetadataGenerationError("psycopg2 is required for observed ranges") from exc

        result: dict[str, dict[str, str]] = defaultdict(dict)
        try:
            connection = psycopg2.connect(
                dbname=database,
                user=self.config.pg_user,
                password=self.config.pg_password,
                host=self.config.pg_host,
                port=self.config.pg_port,
                connect_timeout=10,
            )
        except Exception as exc:
            raise MetadataGenerationError(f"cannot connect to PostgreSQL database {database!r}: {exc}") from exc
        try:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                for table in tables:
                    table_name = table["table_name"]
                    try:
                        cursor.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = %s",
                            (table_name,),
                        )
                        actual_columns = {
                            str(actual).casefold(): str(actual)
                            for (actual,) in cursor.fetchall()
                        }
                    except Exception as exc:
                        connection.rollback()
                        raise MetadataGenerationError(
                            f"cannot inspect PostgreSQL columns for {database}.{table_name}: {exc}"
                        ) from exc
                    missing_columns = [
                        column["column_name"]
                        for column in table["columns"]
                        if column["column_name"].casefold() not in actual_columns
                    ]
                    if missing_columns:
                        raise MetadataGenerationError(
                            f"schema column(s) missing in PostgreSQL {database}.{table_name}: "
                            + ", ".join(missing_columns)
                        )
                    for column in table["columns"]:
                        column_name = column["column_name"]
                        actual_column_name = actual_columns[column_name.casefold()]
                        query = sql.SQL(
                            "SELECT MIN({column}), MAX({column}) "
                            "FROM {table}"
                        ).format(
                            column=sql.Identifier(actual_column_name),
                            table=sql.Identifier(table_name),
                        )
                        try:
                            cursor.execute(query)
                            low, high = cursor.fetchone()
                        except Exception as exc:
                            # jsonb/array-like types may not define a native
                            # ordering aggregate.  A text fallback still
                            # records an observed lexical range, while native
                            # numeric/date/time columns retain their real
                            # ordering and precision.
                            connection.rollback()
                            fallback = sql.SQL(
                                "SELECT MIN(CAST({column} AS TEXT)), MAX(CAST({column} AS TEXT)) "
                                "FROM {table}"
                            ).format(
                                column=sql.Identifier(actual_column_name),
                                table=sql.Identifier(table_name),
                            )
                            try:
                                cursor.execute(fallback)
                                low, high = cursor.fetchone()
                            except Exception as fallback_exc:
                                connection.rollback()
                                raise MetadataGenerationError(
                                    f"cannot read observed range for {database}.{table_name}.{column_name}: "
                                    f"{fallback_exc}"
                                ) from fallback_exc
                        if low is None and high is None:
                            result[table_name][column_name] = ""
                        elif low == high:
                            result[table_name][column_name] = f"value={low}"
                        else:
                            result[table_name][column_name] = f"min={low}; max={high}"
            connection.commit()
        finally:
            connection.close()
        return {table: dict(columns) for table, columns in result.items()}


def source_hash(source_root: Path) -> str:
    """Hash only the allowed source files, including relative file names."""
    paths = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith("_schema.txt")
            or path.name.endswith("_column_meaning_base.json")
            or path.name.endswith("_kb.jsonl")
            or path.name == "bird_interact_data.jsonl"
        )
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(source_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_database_metadata(
    database: str,
    tables: list[dict[str, Any]],
    meanings: Mapping[tuple[str, str], str],
    knowledge: list[dict[str, str]],
    ambiguities: list[dict[str, str]],
    observed_ranges: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    dataset, relevant = _semantic_fragments(database, tables, meanings, knowledge, ambiguities)
    table_structures = {table["table_name"]: _structure(table) for table in tables}
    metadata: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "database": database,
        "formulas": dataset["formulas"],
        "classification_rules": dataset["classification_rules"],
        "table_structure": table_structures,
        "disambiguation_rules": dataset["disambiguation_rules"],
        # The first metadata pass intentionally does not infer discrepancies.
        "known_discrepancies": [],
        "tables": [],
    }
    for table in tables:
        table_name = table["table_name"]
        table_key = f"{database}:{table_name}"
        table_structure = table_structures[table_name]
        table_payload: dict[str, Any] = {
            "table_id": table_key,
            "table_name": table_name,
            "metadata": {
                "table_structure": table_structure,
            },
            "columns": [],
        }
        for column in table["columns"]:
            column_name = column["column_name"]
            column_id = f"{table_key}:{column_name}"
            meaning = meanings.get((_compact(table_name), _compact(column_name)), "")
            identity = []
            if column["is_primary_key"] or re.search(r"(?:^|_)(?:id|ref|key|code|registry|trace|join)(?:$|_)", column_name):
                identity.append(_fragment(
                    f"{column_name} identifies or references a row in {table_name}."
                    + (f" {meaning}" if meaning else "")
                ))
            column_payload = {
                "column_id": column_id,
                "column_name": column_name,
                "column_type": column["column_type"],
                "description": meaning,
                "metadata": {
                    **relevant.get(f"{table_name}:{column_name}", {}),
                    "column_identity_references": identity,
                    "observed_value_range": observed_ranges.get(table_name, {}).get(column_name, ""),
                },
            }
            # Keep generated empty arrays explicit for stable types and make
            # the no-discrepancy policy visible in every returned column.
            for field in ("formulas", "classification_rules", "disambiguation_rules", "known_discrepancies"):
                column_payload["metadata"].setdefault(field, [])
            column_payload["metadata"].setdefault("table_structure", table_structure)
            table_payload["columns"].append(column_payload)
        metadata["tables"].append(table_payload)
    return metadata


class MetadataCorpusGenerator:
    def __init__(self, source_root: Path, observed_reader: PostgreSQLObservedRangeReader | None = None):
        self.source_root = source_root
        self.observed_reader = observed_reader or PostgreSQLObservedRangeReader(settings)

    def _load_database_inputs(self) -> list[dict[str, Any]]:
        task_path = self.source_root / "bird_interact_data.jsonl"
        ambiguities = load_ambiguities(task_path) if task_path.exists() else {}
        databases: list[dict[str, Any]] = []
        for directory in sorted(path for path in self.source_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            database = directory.name.casefold()
            schema_path = directory / f"{directory.name}_schema.txt"
            meanings_path = directory / f"{directory.name}_column_meaning_base.json"
            knowledge_path = directory / f"{directory.name}_kb.jsonl"
            if not schema_path.exists():
                continue
            for required in (meanings_path, knowledge_path):
                if not required.exists():
                    raise MetadataGenerationError(f"missing metadata source {required}")
            tables = parse_schema(schema_path)
            meanings = load_column_meanings(meanings_path)
            knowledge = load_knowledge(knowledge_path, database)
            databases.append({
                "database": database,
                "directory": directory,
                "tables": tables,
                "meanings": meanings,
                "knowledge": knowledge,
                "ambiguities": ambiguities.get(database, []),
            })
        if len(databases) != 18:
            raise MetadataGenerationError(f"expected 18 lite databases, found {len(databases)}")
        return databases

    def generate(self) -> dict[str, Any]:
        inputs = self._load_database_inputs()
        # Collect all ranges before constructing or writing any JSON.  A
        # partial corpus is more dangerous than a failed one.
        ranges: dict[str, dict[str, dict[str, str]]] = {}
        for item in inputs:
            database = item["database"]
            ranges[database] = self.observed_reader.read(database, item["tables"])

        outputs: dict[str, dict[str, Any]] = {}
        total_columns = 0
        for item in inputs:
            database = item["database"]
            outputs[database] = build_database_metadata(
                database,
                item["tables"],
                item["meanings"],
                item["knowledge"],
                item["ambiguities"],
                ranges[database],
            )
            total_columns += sum(len(table["columns"]) for table in outputs[database]["tables"])

        manifest = {
            "schema_version": METADATA_SCHEMA_VERSION,
            "generator_model": GENERATOR_MODEL,
            "embedding_model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMENSIONS,
            "metadata_source_hash": source_hash(self.source_root),
            "databases": [item["database"] for item in inputs],
            "column_count": total_columns,
        }
        manifest["manifest_hash"] = canonical_manifest_hash(manifest)
        return {"manifest": manifest, "databases": outputs}

    @staticmethod
    def write(output: dict[str, Any], source_root: Path) -> None:
        manifest = output["manifest"]
        databases = output["databases"]
        for database, metadata in databases.items():
            target = source_root / database / f"{database}_metadata.json"
            target.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (source_root / "metadata_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=settings.data_dir)
    args = parser.parse_args()
    source = args.source.resolve()
    try:
        result = MetadataCorpusGenerator(source).generate()
        MetadataCorpusGenerator.write(result, source)
    except MetadataGenerationError as exc:
        print(f"metadata generation failed: {exc}", file=sys.stderr)
        return 1
    manifest = result["manifest"]
    print(json.dumps({
        "databases": len(manifest["databases"]),
        "columns": manifest["column_count"],
        "metadata_source_hash": manifest["metadata_source_hash"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
