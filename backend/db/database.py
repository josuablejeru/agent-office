"""SQLite engine creation, schema initialisation and additive migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event, inspect, text
from sqlmodel import SQLModel, create_engine

from backend.db import models  # noqa: F401  (registers tables on SQLModel.metadata)
from backend.logging_config import get_logger

log = get_logger("db")

BUSY_TIMEOUT_SECONDS = 30


def create_db_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, _record: Any) -> None:
        # Runs keep a session open while API requests write: WAL lets readers and
        # a writer coexist, and the busy timeout makes writers queue, not fail.
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.close()

    return engine


def _default_clause(column: Any) -> str:
    """SQL DEFAULT for a column being added to a table that already has rows."""
    default = column.default.arg if column.default is not None else None
    if isinstance(default, bool):
        return f" DEFAULT {int(default)}"
    if isinstance(default, (int, float)):
        return f" DEFAULT {default}"
    if isinstance(default, str):
        return " DEFAULT '" + default.replace("'", "''") + "'"
    return ""


def migrate(engine: Engine) -> list[str]:
    """Add columns the models have gained since the database was created.

    Only additive changes are handled, which is all v0.x needs: existing data is
    never rewritten or dropped. Returns the columns that were added.
    """
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                default = _default_clause(column)
                column_type = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{default}')
                )
                if not default and column.default is not None and column.default.is_callable:
                    # SQLite cannot add a column with a computed default (such as
                    # "now"), so existing rows are filled in afterwards.
                    connection.execute(table.update().values({column.name: column.default.arg(None)}))
                added.append(f"{table.name}.{column.name}")
    if added:
        log.info("database migrated", extra={"added": added})
    return added


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
    migrate(engine)
