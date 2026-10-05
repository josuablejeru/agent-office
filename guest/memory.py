"""The agent's long-term memory: facts about subjects, optionally linked to each other.

One SQLite file on the agent's own disk. Each entry is about a subject and
holds a note, a link to another thing (relation + object), or both. The links
form a small graph, so recalling one thing also brings up what it is connected
to. Search uses SQLite's full-text index when available.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from guest.database import describe_databases
from guest.sqlite_util import in_thread
from guest.validation import OperationError, optional_number, optional_str

# Outside ~/Databases on purpose: the SQL tool cannot name this file.
MEMORY_DB = Path.home() / ".agent-office" / "memory.sqlite"
MAX_FIELD_CHARS = 300
MAX_NOTE_CHARS = 2000
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
CONTEXT_FACTS = 8
MAX_SEARCH_WORDS = 12
WORD = re.compile(r"\w+", re.UNICODE)
# Words too common to say anything about which memory is relevant.
STOP_WORDS = frozenset(
    "a an and are as at be but by can do for from have how i in is it me my of on or our please "
    "that the their them then there this to was we what when where which who will with you your".split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT '',
    object TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS facts_identity
    ON facts (lower(subject), lower(relation), lower(object));
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(subject, relation, object, note, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, relation, object, note)
    VALUES (new.id, new.subject, new.relation, new.object, new.note);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, relation, object, note)
    VALUES ('delete', old.id, old.subject, old.relation, old.object, old.note);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, relation, object, note)
    VALUES ('delete', old.id, old.subject, old.relation, old.object, old.note);
    INSERT INTO facts_fts(rowid, subject, relation, object, note)
    VALUES (new.id, new.subject, new.relation, new.object, new.note);
END;
"""
COLUMNS = "id, subject, relation, object, note, updated_at"


def search_words(text: str) -> list[str]:
    """The words of a free-text query worth searching for."""
    words = [word.lower() for word in WORD.findall(text)]
    meaningful = [word for word in words if word not in STOP_WORDS and len(word) > 1]
    return list(dict.fromkeys(meaningful))[:MAX_SEARCH_WORDS]


def fts_query(words: list[str]) -> str:
    # Each word quoted, so punctuation and words like AND cannot be read as query syntax.
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


def prepare(connection: sqlite3.Connection) -> bool:
    """Create the tables. Returns whether full-text search is available."""
    connection.executescript(SCHEMA)
    try:
        connection.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        return False
    return True


def as_fact(row: tuple[Any, ...]) -> dict[str, Any]:
    fact: dict[str, Any] = {"id": row[0], "subject": row[1]}
    if row[2]:
        fact["relation"], fact["object"] = row[2], row[3]
    if row[4]:
        fact["note"] = row[4]
    fact["updated"] = int(row[5])
    return fact


def search(connection: sqlite3.Connection, has_fts: bool, text: str, limit: int) -> list[tuple[Any, ...]]:
    words = search_words(text)
    if not words:
        return connection.execute(
            f"SELECT {COLUMNS} FROM facts ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    if has_fts:
        return connection.execute(
            f"SELECT {', '.join('f.' + c.strip() for c in COLUMNS.split(','))} FROM facts_fts "
            "JOIN facts f ON f.id = facts_fts.rowid WHERE facts_fts MATCH ? "
            "ORDER BY bm25(facts_fts), f.updated_at DESC LIMIT ?",
            (fts_query(words), limit),
        ).fetchall()
    clauses = " OR ".join(
        "(lower(subject) LIKE ? OR lower(relation) LIKE ? OR lower(object) LIKE ? OR lower(note) LIKE ?)"
        for _ in words
    )
    params = [f"%{word}%" for word in words for _ in range(4)]
    return connection.execute(
        f"SELECT {COLUMNS} FROM facts WHERE {clauses} ORDER BY updated_at DESC LIMIT ?", (*params, limit)
    ).fetchall()


def about(connection: sqlite3.Connection, thing: str, limit: int) -> list[tuple[Any, ...]]:
    """Everything connected to `thing`, following links up to two steps away."""
    return connection.execute(
        f"""
        WITH RECURSIVE reached(name, depth) AS (
            SELECT lower(?), 0
            UNION
            SELECT lower(CASE WHEN lower(f.subject) = reached.name THEN f.object ELSE f.subject END),
                   reached.depth + 1
            FROM facts f JOIN reached
              ON lower(f.subject) = reached.name OR (f.object != '' AND lower(f.object) = reached.name)
            WHERE reached.depth < 2 AND f.object != ''
        )
        SELECT {', '.join('f.' + c.strip() for c in COLUMNS.split(','))}
        FROM facts f JOIN reached
          ON lower(f.subject) = reached.name OR (f.object != '' AND lower(f.object) = reached.name)
        GROUP BY f.id
        ORDER BY MIN(reached.depth), f.updated_at DESC LIMIT ?
        """,
        (thing, limit),
    ).fetchall()


def _text(args: dict[str, Any], key: str, limit: int) -> str:
    """An optional text field; missing, null and empty all mean "not given"."""
    if args.get(key) in (None, ""):
        return ""
    return (optional_str(args, key, limit) or "").strip()


async def memory_remember(args: dict[str, Any]) -> dict[str, Any]:
    subject = _text(args, "subject", MAX_FIELD_CHARS)
    relation = _text(args, "relation", MAX_FIELD_CHARS)
    linked = _text(args, "object", MAX_FIELD_CHARS)
    note = _text(args, "note", MAX_NOTE_CHARS)
    if not subject:
        raise OperationError("'subject' says what or who the memory is about and is required")
    if bool(relation) != bool(linked):
        raise OperationError("a link needs both 'relation' and 'object' (for example works_at + Acme)")
    if not note and not relation:
        raise OperationError("give a 'note', or a link ('relation' and 'object'), or both")

    def work(connection: sqlite3.Connection) -> dict[str, Any]:
        prepare(connection)
        now = time.time()
        existing = connection.execute(
            "SELECT id FROM facts WHERE lower(subject)=lower(?) AND lower(relation)=lower(?) AND lower(object)=lower(?)",
            (subject, relation, linked),
        ).fetchone()
        # A bare note about a subject adds to what is known; the same link is updated in place.
        if existing and relation:
            connection.execute(
                "UPDATE facts SET note = CASE WHEN ? != '' THEN ? ELSE note END, updated_at = ? WHERE id = ?",
                (note, note, now, existing[0]),
            )
            return {"id": existing[0], "updated": True}
        if existing and not relation:
            same = connection.execute("SELECT note FROM facts WHERE id = ?", (existing[0],)).fetchone()[0]
            merged = same if note in same else f"{same}\n{note}"
            connection.execute(
                "UPDATE facts SET note = ?, updated_at = ? WHERE id = ?",
                (merged[:MAX_NOTE_CHARS], now, existing[0]),
            )
            return {"id": existing[0], "updated": True}
        cursor = connection.execute(
            "INSERT INTO facts (subject, relation, object, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (subject, relation, linked, note, now, now),
        )
        return {"id": cursor.lastrowid, "updated": False}

    return await in_thread(MEMORY_DB, work)


async def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
    query = _text(args, "query", 500)
    thing = _text(args, "about", MAX_FIELD_CHARS)
    limit = int(optional_number(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT))

    def work(connection: sqlite3.Connection) -> dict[str, Any]:
        has_fts = prepare(connection)
        rows = about(connection, thing, limit) if thing else search(connection, has_fts, query, limit)
        total = connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return {"facts": [as_fact(row) for row in rows], "total_remembered": total}

    return await in_thread(MEMORY_DB, work)


async def memory_forget(args: dict[str, Any]) -> dict[str, Any]:
    fact_id = args.get("id")
    if isinstance(fact_id, bool) or not isinstance(fact_id, int):
        raise OperationError("'id' must be the number of a remembered entry")

    def work(connection: sqlite3.Connection) -> dict[str, Any]:
        prepare(connection)
        deleted = connection.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount
        if not deleted:
            raise OperationError(f"there is no remembered entry {fact_id}")
        return {"forgotten": fact_id}

    return await in_thread(MEMORY_DB, work)


async def memory_context(args: dict[str, Any]) -> dict[str, Any]:
    """What to put in front of the model at the start of a task."""
    task = _text(args, "task", 4000)

    def work(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        has_fts = prepare(connection)
        rows = search(connection, has_fts, task, CONTEXT_FACTS)
        if len(rows) < CONTEXT_FACTS:
            # Top up with the most recent entries: recent context is often relevant too.
            seen = {row[0] for row in rows}
            recent = search(connection, has_fts, "", CONTEXT_FACTS)
            rows += [row for row in recent if row[0] not in seen][: CONTEXT_FACTS - len(rows)]
        return [as_fact(row) for row in rows]

    facts = await in_thread(MEMORY_DB, work)
    return {"facts": facts, "databases": await asyncio.to_thread(describe_databases)}
