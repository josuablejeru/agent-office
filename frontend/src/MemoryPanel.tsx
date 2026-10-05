import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Agent, MemoryView } from "./types";

const REFRESH_MS = 5000;

/** What the agent remembers and the databases it keeps. You can add to it and remove from it. */
export function MemoryPanel({ agent }: { agent: Agent }) {
  const [memory, setMemory] = useState<MemoryView | null>(null);
  const [query, setQuery] = useState("");
  const [subject, setSubject] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error`: a background refresh must not erase what just went wrong.
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setMemory(await api.getMemory(agent.id, query));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [agent.id, query]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (!subject.trim() || !note.trim()) return;
    try {
      await api.addFact(agent.id, { subject: subject.trim(), note: note.trim(), relation: "", object: "" });
      setSubject("");
      setNote("");
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function forget(id: number) {
    try {
      await api.forgetFact(agent.id, id);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  const tables = memory?.databases.flatMap((db) => db.tables.map((table) => ({ db: db.name, ...table }))) ?? [];

  return (
    <section className="computer memory">
      <div className="computer-bar">
        <span className="computer-title">
          What {agent.name} remembers
          {memory && <span className="muted"> · {memory.total_remembered} entries</span>}
        </span>
        <input
          className="memory-search"
          type="search"
          placeholder="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {error && <div className="notice error">{error}</div>}
      {actionError && (
        <div className="notice error" onClick={() => setActionError(null)} title="Dismiss">
          {actionError}
        </div>
      )}
      <div className="file-list">
        {memory?.facts.length === 0 && (
          <p className="hint">
            {query
              ? "Nothing matches."
              : "Nothing yet. The agent saves what is worth knowing later, and you can add entries below. " +
                "Memory lives on the agent's computer and is kept when you clear the conversation."}
          </p>
        )}
        {memory?.facts.map((fact) => (
          <div key={fact.id} className="fact-row">
            <div className="fact-body">
              <strong>{fact.subject}</strong>
              {fact.relation && (
                <span className="fact-link">
                  {" "}
                  {fact.relation} <strong>{fact.object}</strong>
                </span>
              )}
              {fact.note && <div className="fact-note">{fact.note}</div>}
            </div>
            <button title="Remove this entry" onClick={() => void forget(fact.id)}>
              Forget
            </button>
          </div>
        ))}

        {tables.length > 0 && (
          <>
            <h3 className="panel-heading">Databases</h3>
            {tables.map((table) => (
              <div key={`${table.db}.${table.name}`} className="file-row">
                <span className="file-name" title={table.columns.join(", ")}>
                  {table.db} · <strong>{table.name}</strong>{" "}
                  <span className="muted">({table.columns.join(", ")})</span>
                </span>
                <span className="muted">{table.rows} rows</span>
              </div>
            ))}
          </>
        )}
      </div>
      <form className="memory-add" onSubmit={(e) => void add(e)}>
        <input placeholder="About (e.g. Dana)" value={subject} onChange={(e) => setSubject(e.target.value)} />
        <input placeholder="What to remember" value={note} onChange={(e) => setNote(e.target.value)} />
        <button type="submit" disabled={!subject.trim() || !note.trim()}>
          Add
        </button>
      </form>
    </section>
  );
}
