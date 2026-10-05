"""db.sql: SQLite databases the agent creates and owns, under ~/Databases."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

from guest.sqlite_util import MAX_ROWS, connect, in_thread, json_safe
from guest.validation import OperationError, require_str

DATABASES_DIR = Path.home() / "Databases"
DATABASE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
DEFAULT_DATABASE = "main"
MAX_SQL_CHARS = 20_000
MAX_PARAMS = 200


def database_path(args: dict[str, Any]) -> Path:
    name = args.get("database") or DEFAULT_DATABASE
    if not isinstance(name, str) or not DATABASE_NAME.match(name):
        raise OperationError("a database name uses lowercase letters, digits, '-' and '_'")
    return DATABASES_DIR / f"{name}.sqlite"


async def db_sql(args: dict[str, Any]) -> dict[str, Any]:
    path = database_path(args)
    sql = require_str(args, "sql", MAX_SQL_CHARS)
    params = args.get("params") or []
    if not isinstance(params, list) or len(params) > MAX_PARAMS:
        raise OperationError("'params' must be a list of values for the ? placeholders")
    # A list of rows runs the statement once per row: the natural way to insert several records.
    many = bool(params) and all(isinstance(row, list) for row in params)
    flat = [value for row in params for value in row] if many else params
    if any(isinstance(value, (dict, list)) for value in flat):
        raise OperationError(
            "'params' must be a list of values, or a list of rows (each a list of values) "
            "to run the statement once per row"
        )

    def work(connection: sqlite3.Connection) -> dict[str, Any]:
        try:
            cursor = connection.executemany(sql, params) if many else connection.execute(sql, params)
        except sqlite3.ProgrammingError as exc:
            if "one statement at a time" in str(exc):
                raise OperationError("run one SQL statement per call") from exc
            raise
        if cursor.description is None:
            return {"database": path.stem, "rows_affected": cursor.rowcount}
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchmany(MAX_ROWS + 1)
        return {
            "database": path.stem,
            "columns": columns,
            "rows": [[json_safe(value) for value in row] for row in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS,
        }

    return await in_thread(path, work)


def describe_databases() -> list[dict[str, Any]]:
    """Every database with its tables, columns and row counts (for recall and the app)."""
    described = []
    for path in sorted(DATABASES_DIR.glob("*.sqlite")):
        try:
            connection = connect(path)
            tables = []
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for (table,) in names[:30]:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
                count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                tables.append({"name": table, "columns": columns, "rows": count})
            connection.close()
        except sqlite3.Error:
            continue  # not a usable database: leave it out rather than fail the listing
        described.append({"name": path.stem, "tables": tables})
    return described


async def db_list(args: dict[str, Any]) -> dict[str, Any]:
    return {"databases": await asyncio.to_thread(describe_databases)}
