"""Compare the local fEMR schema with the central RDS schema.

This script is intentionally conservative: it reads both live schemas, reports
drift, and can emit safe SQL suggestions for missing RDS columns. It does not
apply changes automatically because the two sides are managed by different
systems (Play evolutions on fEMR and Django models/migrations on central RDS).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional

import pymysql
import re
import glob
import ast


LOCAL_TO_RDS_TABLES = {
    "patients": "central_api_patient",
    "patient_encounters": "central_api_patientencounter",
    "users": "central_api_user",
    "mission_trips": "central_api_missiontrip",
    "photos": "central_api_photo",
    "patient_encounter_photos": "central_api_patientencounterphoto",
}


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    column_type: str
    is_nullable: str
    column_default: Optional[str]
    extra: str
    ordinal_position: int


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: Dict[str, ColumnSchema]


@dataclass(frozen=True)
class TableDiff:
    local_table: str
    rds_table: str
    missing_in_local: List[str]
    missing_in_rds: List[str]
    type_mismatches: Dict[str, Dict[str, str]]
    nullable_mismatches: Dict[str, Dict[str, str]]

    @property
    def has_drift(self) -> bool:
        return bool(
            self.missing_in_local
            or self.missing_in_rds
            or self.type_mismatches
            or self.nullable_mismatches
        )


@dataclass(frozen=True)
class SchemaComparisonResult:
    local_schemas: Dict[str, TableSchema]
    rds_schemas: Dict[str, TableSchema]
    diffs: List[TableDiff]


def _connect_from_env(prefix: str) -> pymysql.connections.Connection:
    host = os.environ.get(f"{prefix}_HOST")
    database = os.environ.get(f"{prefix}_DB_NAME")
    username = os.environ.get(f"{prefix}_USERNAME", os.environ.get(f"{prefix}_USER"))
    password = os.environ.get(f"{prefix}_PASSWORD", os.environ.get(f"{prefix}_PASS"))
    port = int(os.environ.get(f"{prefix}_PORT", "3306"))

    if not host:
        raise ValueError(f"{prefix}_HOST is required")
    if not database:
        raise ValueError(f"{prefix}_DB_NAME is required")
    if not username:
        raise ValueError(f"{prefix}_USERNAME or {prefix}_USER is required")
    if password is None:
        raise ValueError(f"{prefix}_PASSWORD or {prefix}_PASS is required")

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_table_schema(cursor, table_name: str) -> TableSchema:
    cursor.execute(
        """
        SELECT
            COLUMN_NAME,
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            EXTRA,
            ORDINAL_POSITION
        FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table_name,),
    )

    rows = cursor.fetchall()
    columns = {
        row["COLUMN_NAME"]: ColumnSchema(
            name=row["COLUMN_NAME"],
            column_type=row["COLUMN_TYPE"],
            is_nullable=row["IS_NULLABLE"],
            column_default=row["COLUMN_DEFAULT"],
            extra=row["EXTRA"],
            ordinal_position=int(row["ORDINAL_POSITION"]),
        )
        for row in rows
    }
    return TableSchema(name=table_name, columns=columns)


def load_schema(cursor, table_names: Iterable[str]) -> Dict[str, TableSchema]:
    return {table_name: load_table_schema(cursor, table_name) for table_name in table_names}


def compare_table_schemas(local: TableSchema, rds: TableSchema) -> TableDiff:
    local_columns = local.columns
    rds_columns = rds.columns

    shared_columns = sorted(set(local_columns) & set(rds_columns))
    missing_in_local = sorted(set(rds_columns) - set(local_columns))
    missing_in_rds = sorted(set(local_columns) - set(rds_columns))

    type_mismatches: Dict[str, Dict[str, str]] = {}
    nullable_mismatches: Dict[str, Dict[str, str]] = {}

    for column_name in shared_columns:
        local_column = local_columns[column_name]
        rds_column = rds_columns[column_name]

        if local_column.column_type.lower() != rds_column.column_type.lower():
            type_mismatches[column_name] = {
                "local": local_column.column_type,
                "rds": rds_column.column_type,
            }

        if local_column.is_nullable != rds_column.is_nullable:
            nullable_mismatches[column_name] = {
                "local": local_column.is_nullable,
                "rds": rds_column.is_nullable,
            }

    return TableDiff(
        local_table=local.name,
        rds_table=rds.name,
        missing_in_local=missing_in_local,
        missing_in_rds=missing_in_rds,
        type_mismatches=type_mismatches,
        nullable_mismatches=nullable_mismatches,
    )


def _sql_literal(value: Optional[str]) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def generate_add_column_sql(diff: TableDiff, local_schema: TableSchema) -> List[str]:
    statements: List[str] = []
    for column_name in diff.missing_in_rds:
        column = local_schema.columns[column_name]
        null_sql = "NULL" if column.is_nullable == "YES" else "NOT NULL"
        default_sql = ""
        if column.column_default is not None:
            default_sql = f" DEFAULT {_sql_literal(column.column_default)}"

        extra_sql = f" {column.extra}" if column.extra else ""
        statements.append(
            f"ALTER TABLE {diff.rds_table} ADD COLUMN {column.name} {column.column_type} {null_sql}{default_sql}{extra_sql};"
        )

    return statements


def compare_schemas() -> SchemaComparisonResult:
    local_conn = _connect_from_env("DB")
    rds_conn = _connect_from_env("RDS")

    try:
        with local_conn.cursor() as local_cursor, rds_conn.cursor() as rds_cursor:
            local_schemas = load_schema(local_cursor, LOCAL_TO_RDS_TABLES.keys())
            rds_schemas = load_schema(rds_cursor, LOCAL_TO_RDS_TABLES.values())

        diffs: List[TableDiff] = []
        for local_table, rds_table in LOCAL_TO_RDS_TABLES.items():
            diffs.append(compare_table_schemas(local_schemas[local_table], rds_schemas[rds_table]))

        return SchemaComparisonResult(
            local_schemas=local_schemas,
            rds_schemas=rds_schemas,
            diffs=diffs,
        )
    finally:
        local_conn.close()
        rds_conn.close()


def parse_play_evolutions(evolutions_dir: str) -> Dict[str, TableSchema]:
    sql_files = sorted(glob.glob(os.path.join(evolutions_dir, "*.sql")))
    tables: Dict[str, TableSchema] = {}

    create_re = re.compile(r"CREATE TABLE\s+`(?P<name>[^`]+)`\s*\((?P<body>.*?)\)", re.S | re.I)
    col_re = re.compile(r"^\s*`(?P<name>[^`]+)`\s+(?P<type>[^,]+)(?P<rest>)$")

    for sql_file in sql_files:
        with open(sql_file, "r", encoding="utf-8") as fh:
            content = fh.read()

        for m in create_re.finditer(content):
            tbl = m.group("name")
            body = m.group("body")
            columns: Dict[str, ColumnSchema] = {}
            ordinal = 1
            for line in body.splitlines():
                line = line.strip().rstrip(',')
                if not line:
                    continue
                cm = col_re.match(line)
                if not cm:
                    continue
                name = cm.group("name")
                type_part = cm.group("type").strip()
                rest = line[len(cm.group(0)):]
                is_nullable = "NO" if "NOT NULL" in line.upper() else "YES"
                default = None
                dmatch = re.search(r"DEFAULT\s+([^\s,]+)", line, re.I)
                if dmatch:
                    default = dmatch.group(1).strip().strip("'\"")

                extra = "AUTO_INCREMENT" if "AUTO_INCREMENT" in line.upper() else ""

                columns[name] = ColumnSchema(
                    name=name,
                    column_type=type_part,
                    is_nullable=is_nullable,
                    column_default=default,
                    extra=extra,
                    ordinal_position=ordinal,
                )
                ordinal += 1

            tables[tbl] = TableSchema(name=tbl, columns=columns)

    return tables


def _django_field_to_sql(field_call: ast.Call) -> (str, str, Optional[str], str):
    # return (column_type, is_nullable, default, extra)
    func = field_call.func
    if isinstance(func, ast.Attribute):
        field_type = func.attr
    elif isinstance(func, ast.Name):
        field_type = func.id
    else:
        field_type = "Unknown"

    kw = {k.arg: k.value for k in field_call.keywords}
    is_nullable = "YES"
    if 'null' in kw:
        if isinstance(kw['null'], ast.Constant) and kw['null'].value is True:
            is_nullable = "YES"
        else:
            is_nullable = "NO"
    if 'blank' in kw:
        # blank doesn't affect SQL nullability directly
        pass

    default = None
    if 'default' in kw:
        v = kw['default']
        if isinstance(v, ast.Constant):
            default = v.value

    # best-effort type mapping
    mapping = {
        'IntegerField': 'int(11)',
        'AutoField': 'int(11) AUTO_INCREMENT',
        'BigIntegerField': 'bigint',
        'CharField': 'varchar(255)',
        'TextField': 'text',
        'BinaryField': 'blob',
        'DateTimeField': 'datetime',
        'BooleanField': 'tinyint(1)',
        'ForeignKey': 'int(11)',
        'OneToOneField': 'int(11)',
    }

    col_type = mapping.get(field_type, field_type)
    extra = ''
    return col_type, is_nullable, default, extra


def parse_django_migrations(migrations_dir: str) -> Dict[str, TableSchema]:
    py_files = sorted(glob.glob(os.path.join(migrations_dir, "*.py")))
    tables: Dict[str, TableSchema] = {}

    for py_file in py_files:
        try:
            with open(py_file, "r", encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=py_file)
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if getattr(node.func, 'attr', '') == 'CreateModel':
                    name = None
                    fields_node = None
                    for kw in node.keywords:
                        if kw.arg == 'name' and isinstance(kw.value, ast.Constant):
                            name = kw.value.value
                        if kw.arg == 'fields':
                            fields_node = kw.value

                    if not name or fields_node is None:
                        continue

                    columns: Dict[str, ColumnSchema] = {}
                    ordinal = 1
                    if isinstance(fields_node, (ast.List, ast.Tuple)):
                        for elt in fields_node.elts:
                            # each elt is a Tuple(name, fieldcall)
                            if not isinstance(elt, ast.Tuple) or len(elt.elts) < 2:
                                continue
                            field_name_node = elt.elts[0]
                            field_call = elt.elts[1]
                            if not isinstance(field_name_node, ast.Constant):
                                continue
                            fname = field_name_node.value
                            if isinstance(field_call, ast.Call):
                                col_type, is_nullable, default, extra = _django_field_to_sql(field_call)
                            else:
                                col_type, is_nullable, default, extra = ('unknown', 'YES', None, '')

                            columns[fname] = ColumnSchema(
                                name=fname,
                                column_type=col_type,
                                is_nullable=is_nullable,
                                column_default=default,
                                extra=extra,
                                ordinal_position=ordinal,
                            )
                            ordinal += 1

                    tables[name.lower()] = TableSchema(name=name.lower(), columns=columns)

    return tables


def compare_static_schemas(play_dir: str, django_dir: str) -> SchemaComparisonResult:
    local_schemas = parse_play_evolutions(play_dir)
    rds_schemas = parse_django_migrations(django_dir)

    # map local table names to rds names using LOCAL_TO_RDS_TABLES
    diffs: List[TableDiff] = []
    for local_table, rds_table in LOCAL_TO_RDS_TABLES.items():
        ltbl = local_table
        rtbl = rds_table
        if ltbl not in local_schemas:
            # create an empty placeholder
            local_schema = TableSchema(name=ltbl, columns={})
        else:
            local_schema = local_schemas[ltbl]

        # migrations use model names; central migration tables may be prefixed differently
        rname = rtbl.replace('central_api_', '')
        if rname in rds_schemas:
            r_schema = rds_schemas[rname]
        elif rtbl in rds_schemas:
            r_schema = rds_schemas[rtbl]
        else:
            # try model name from table
            r_schema = TableSchema(name=rtbl, columns={})

        diffs.append(compare_table_schemas(local_schema, r_schema))

    return SchemaComparisonResult(local_schemas=local_schemas, rds_schemas=rds_schemas, diffs=diffs)


def render_report(result: SchemaComparisonResult, emit_sql: bool = False) -> int:
    drift_found = False

    for diff in result.diffs:
        print(f"{diff.local_table} -> {diff.rds_table}")
        if not diff.has_drift:
            print("  OK: schemas match")
            continue

        drift_found = True
        if diff.missing_in_local:
            print(f"  Missing in local: {', '.join(diff.missing_in_local)}")
        if diff.missing_in_rds:
            print(f"  Missing in RDS: {', '.join(diff.missing_in_rds)}")
        if diff.type_mismatches:
            print(f"  Type mismatches: {json.dumps(diff.type_mismatches, sort_keys=True)}")
        if diff.nullable_mismatches:
            print(f"  Nullable mismatches: {json.dumps(diff.nullable_mismatches, sort_keys=True)}")

        if emit_sql:
            for statement in generate_add_column_sql(diff, result.local_schemas[diff.local_table]):
                print(f"  SQL: {statement}")

    return 1 if drift_found else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare fEMR and central RDS schemas.")
    parser.add_argument(
        "--emit-sql",
        action="store_true",
        help="Print ALTER TABLE statements for missing RDS columns.",
    )
    parser.add_argument("--json", action="store_true", help="Print the diff as JSON.")
    parser.add_argument(
        "--play-evolutions",
        help="Path to Play evolutions directory to parse statically (no DB).",
    )
    parser.add_argument(
        "--django-migrations",
        help="Path to Django migrations directory to parse statically (no DB).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.play_evolutions and args.django_migrations:
        result = compare_static_schemas(args.play_evolutions, args.django_migrations)
    else:
        result = compare_schemas()

    if args.json:
        print(
            json.dumps(
                {
                    "diffs": [asdict(diff) for diff in result.diffs],
                    "has_drift": any(diff.has_drift for diff in result.diffs),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if any(diff.has_drift for diff in result.diffs) else 0

    return render_report(result, emit_sql=args.emit_sql)


if __name__ == "__main__":
    raise SystemExit(main())