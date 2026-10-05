"""Agent memory and databases (the guest-side operations)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from guest import database, memory, sqlite_util
from guest.agentd import handle_message
from guest.validation import OperationError


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "MEMORY_DB", tmp_path / "hidden" / "memory.sqlite")
    monkeypatch.setattr(database, "DATABASES_DIR", tmp_path / "Databases")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def remember(**fields: Any) -> dict[str, Any]:
    return run(memory.memory_remember(fields))


def recall(**fields: Any) -> list[dict[str, Any]]:
    return run(memory.memory_recall(fields))["facts"]


def subjects(facts: list[dict[str, Any]]) -> list[str]:
    return [fact["subject"] for fact in facts]


def test_remember_and_recall_by_words() -> None:
    remember(subject="Dana", note="Our accountant. Email dana@example.com, prefers calls before noon.")
    remember(subject="Office wifi", note="Network name Loft-5G")
    remember(subject="Invoices", note="Sent on the first Monday of each month")

    assert subjects(recall(query="who is the accountant?")) == ["Dana"]
    assert subjects(recall(query="wifi network")) == ["Office wifi"]
    assert recall(query="quantum physics") == []
    assert len(recall()) == 3  # no query: the most recent entries
    assert run(memory.memory_recall({}))["total_remembered"] == 3


def test_query_text_cannot_break_the_search() -> None:
    remember(subject="C++", note='The "build" uses -O2 AND NOT -O3: see notes')
    for query in ['"unbalanced', "AND OR NOT", "a:b (c) -d *", "'; DROP TABLE facts; --", "build"]:
        recall(query=query)  # must not raise
    assert subjects(recall(query="build")) == ["C++"]
    assert memory.search_words("What is the the accountant's e-mail?") == ["accountant", "mail"]


def test_links_form_a_graph_two_steps_deep() -> None:
    remember(subject="Dana", relation="works at", object="Acme")
    remember(subject="Acme", relation="located in", object="Zurich")
    remember(subject="Zurich", relation="is in", object="Switzerland")
    remember(subject="Unrelated", note="nothing to do with it")

    found = recall(about="dana")  # case does not matter
    described = {(f["subject"], f.get("relation"), f.get("object")) for f in found}
    assert ("Dana", "works at", "Acme") in described
    assert ("Acme", "located in", "Zurich") in described  # one step further
    assert all(f["subject"] != "Unrelated" for f in found)
    assert found[0]["subject"] == "Dana"  # nearest first
    # and from the other end
    assert ("Dana", "works at", "Acme") in {(f["subject"], f.get("relation"), f.get("object")) for f in recall(about="Acme")}


def test_saving_the_same_thing_again_updates_it() -> None:
    first = remember(subject="Dana", relation="works at", object="Acme")
    again = remember(subject="dana", relation="Works At", object="ACME", note="since 2024")
    assert again == {"id": first["id"], "updated": True}
    note_a = remember(subject="Lukas", note="Likes short answers")
    note_b = remember(subject="Lukas", note="Works on the backend")
    assert note_b["id"] == note_a["id"]
    (lukas,) = recall(query="Lukas")
    assert "short answers" in lukas["note"] and "backend" in lukas["note"]
    assert run(memory.memory_recall({}))["total_remembered"] == 2


def test_forget() -> None:
    kept = remember(subject="Keep", note="stay")
    gone = remember(subject="Drop", note="leave")
    assert run(memory.memory_forget({"id": gone["id"]})) == {"forgotten": gone["id"]}
    assert subjects(recall()) == ["Keep"] and recall(query="leave") == []
    with pytest.raises(OperationError, match="no remembered entry"):
        run(memory.memory_forget({"id": 999}))
    with pytest.raises(OperationError):
        run(memory.memory_forget({"id": "1"}))
    assert kept["id"]


@pytest.mark.parametrize(
    "fields",
    [{}, {"subject": ""}, {"subject": "X"}, {"subject": "X", "relation": "knows"},
     {"subject": "X", "object": "Y"}, {"subject": "X" * 400, "note": "n"}],
)
def test_incomplete_memories_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(OperationError):
        remember(**fields)


def test_context_prefers_relevant_then_recent_and_lists_databases() -> None:
    for index in range(12):
        remember(subject=f"Topic {index}", note=f"filler number {index}")
    remember(subject="Dana", note="Our accountant")
    run(database.db_sql({"sql": "CREATE TABLE leads (name TEXT, email TEXT)"}))
    run(database.db_sql({"sql": "INSERT INTO leads VALUES (?, ?)", "params": ["Ada", "ada@example.com"]}))

    context = run(memory.memory_context({"task": "send the report to our accountant"}))
    assert context["facts"][0]["subject"] == "Dana"
    assert len(context["facts"]) == memory.CONTEXT_FACTS
    assert context["databases"] == [
        {"name": "main", "tables": [{"name": "leads", "columns": ["name", "email"], "rows": 1}]}]
    assert run(memory.memory_context({}))["facts"]  # no task: recent entries


def test_sql_round_trip_and_json_safe_results() -> None:
    sql = lambda **args: run(database.db_sql(args))  # noqa: E731
    assert sql(sql="CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT, data BLOB)") == {
        "database": "main", "rows_affected": -1}
    assert sql(sql="INSERT INTO notes (body, data) VALUES (?, ?)", params=["x" * 5000, None])["rows_affected"] == 1
    sql(sql="INSERT INTO notes (body, data) VALUES ('short', x'00ff10')")
    result = sql(sql="SELECT body, data FROM notes ORDER BY id")
    assert result["columns"] == ["body", "data"] and result["truncated"] is False
    assert len(result["rows"][0][0]) == sqlite_util.MAX_CELL_CHARS + 1  # long text is cut
    assert result["rows"][1] == ["short", "<blob 3 bytes>"]
    json.dumps(result)

    sql(sql="CREATE TABLE n (v)")
    sql(sql="WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500) INSERT INTO n SELECT x FROM c")
    many = sql(sql="SELECT v FROM n")
    assert len(many["rows"]) == sqlite_util.MAX_ROWS and many["truncated"] is True
    assert sql(database="other", sql="SELECT 1 AS one")["rows"] == [[1]]
    assert sorted(p.name for p in database.DATABASES_DIR.glob("*.sqlite")) == ["main.sqlite", "other.sqlite"]


def test_sql_errors_are_reported_not_raised_raw() -> None:
    sql = lambda **args: run(database.db_sql(args))  # noqa: E731
    with pytest.raises(OperationError, match="SQLite: .*syntax error"):
        sql(sql="SELEC 1")
    with pytest.raises(OperationError, match="one"):
        sql(sql="SELECT 1; SELECT 2")
    with pytest.raises(OperationError, match="no such table"):
        sql(sql="SELECT * FROM missing")
    for name in ("../escape", "a/b", "Main", "", ".hidden", "x" * 60):
        if name:
            with pytest.raises(OperationError, match="database name"):
                sql(database=name, sql="SELECT 1")
    with pytest.raises(OperationError, match="params"):
        sql(sql="SELECT ?", params="notalist")
    with pytest.raises(OperationError, match="list of values"):
        sql(sql="SELECT ?", params=[{"a": 1}])


def test_memory_file_is_out_of_reach_of_the_sql_tool() -> None:
    remember(subject="Secret", note="in memory only")
    assert memory.MEMORY_DB.parent != database.DATABASES_DIR
    assert run(database.db_list({}))["databases"] == []


def test_runaway_query_is_stopped_and_the_daemon_stays_responsive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_util, "QUERY_TIMEOUT_SECONDS", 0.4)
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"

    async def scenario() -> tuple[dict[str, Any], float, dict[str, Any]]:
        started = time.monotonic()
        slow = asyncio.create_task(handle_message(json.dumps({"id": 1, "op": "db.sql", "args": {"sql": endless}})))
        await asyncio.sleep(0.05)
        ping_started = time.monotonic()
        pong = await handle_message(json.dumps({"id": 2, "op": "ping"}))
        ping_seconds = time.monotonic() - ping_started
        reply = await slow
        assert time.monotonic() - started < 5
        return reply, ping_seconds, pong

    reply, ping_seconds, pong = run(scenario())
    assert reply["ok"] is False and "was stopped" in reply["error"]
    assert pong["ok"] is True and ping_seconds < 0.2  # answered while the query was still running


def test_several_rows_can_be_inserted_in_one_call() -> None:
    sql = lambda **args: run(database.db_sql(args))  # noqa: E731
    sql(sql="CREATE TABLE leads (name TEXT, status TEXT)")
    inserted = sql(sql="INSERT INTO leads VALUES (?, ?)", params=[["Ada", "new"], ["Grace", "contacted"], ["Alan", "new"]])
    assert inserted["rows_affected"] == 3
    assert sql(sql="SELECT COUNT(*) FROM leads WHERE status = ?", params=["new"])["rows"] == [[2]]
    with pytest.raises(OperationError, match="list of rows"):
        sql(sql="INSERT INTO leads VALUES (?, ?)", params=[["Ada", {"bad": 1}]])
    with pytest.raises(OperationError, match="list of rows"):
        sql(sql="INSERT INTO leads VALUES (?, ?)", params=["Ada", ["mixed"]])


def test_empty_strings_count_as_not_given() -> None:
    remember(subject="Dana", note="accountant", relation="", object="")
    assert subjects(recall(query="", about="")) == ["Dana"]
    assert run(memory.memory_context({"task": ""}))["facts"][0]["subject"] == "Dana"
