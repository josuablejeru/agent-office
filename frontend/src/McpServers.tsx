import { useEffect, useState } from "react";
import { api } from "./api";
import type { McpServer, McpServerForm } from "./types";

const EMPTY: McpServerForm = { name: "", url: "", auth_header: "Authorization", description: "", token: "" };

/** Settings › MCP servers: the one place where MCP servers are set up for every agent. */
export function McpServers() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [form, setForm] = useState<McpServerForm | null>(null);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string; tools?: string[] } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api
      .listMcpServers()
      .then(setServers)
      .catch(() => undefined);
  useEffect(() => void load(), []);

  const set = (key: keyof McpServerForm, value: string) =>
    setForm((current) => (current ? { ...current, [key]: value } : current));

  function open(server: McpServer | null) {
    setEditing(server);
    setResult(null);
    setForm(server ? { ...EMPTY, ...server, token: "" } : EMPTY);
  }

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setResult(null);
    try {
      await action();
    } catch (err) {
      setResult({ ok: false, text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  const test = () =>
    run(async () => {
      if (!form) return;
      const tested = await api.testMcpServer(form);
      setResult({ ok: tested.ok, text: tested.detail, tools: tested.tools });
    });

  const save = () =>
    run(async () => {
      if (!form) return;
      await api.saveMcpServer(form);
      setForm(null);
      setEditing(null);
      await load();
    });

  const remove = (name: string) =>
    run(async () => {
      await api.removeMcpServer(name);
      setForm(null);
      setEditing(null);
      await load();
    });

  return (
    <div className="mcp-servers">
      <p className="hint">
        MCP servers give agents more tools: a ticket system, a wiki, a CRM. Set a server up once here, then tick it
        on each agent&apos;s Permissions tab; its tools ask for your approval unless you allow them there. Agent
        Office connects to the server itself, so tokens stay in your macOS Keychain and never reach an agent&apos;s
        computer. Remote servers (MCP over HTTP) only.
      </p>
      {servers.map((server) => (
        <div key={server.name} className="dns-rule">
          <code>{server.name}</code>
          <span className="muted">
            {server.url}
            {server.has_token ? " · token in Keychain" : ""}
            {server.description ? ` · ${server.description}` : ""}
          </span>
          <button className="link-button" onClick={() => open(server)}>
            Edit
          </button>
        </div>
      ))}
      {servers.length === 0 && !form && <p className="hint">No MCP servers yet.</p>}

      {form ? (
        <div className="mcp-form">
          <div className="row">
            <label>
              Name
              <input
                value={form.name}
                disabled={!!editing}
                placeholder="e.g. tickets"
                pattern="[a-z][a-z0-9\-]{0,31}"
                onChange={(e) => set("name", e.target.value.toLowerCase())}
              />
            </label>
            <label>
              Address
              <input value={form.url} placeholder="https://mcp.example.com/mcp" onChange={(e) => set("url", e.target.value)} />
            </label>
          </div>
          <div className="row">
            <label>
              Token
              <input
                type="password"
                autoComplete="new-password"
                value={form.token}
                placeholder={editing?.has_token ? "Stored in Keychain (leave empty to keep)" : "If the server needs one"}
                onChange={(e) => set("token", e.target.value)}
              />
            </label>
            <label>
              Sent in the header
              <input value={form.auth_header} onChange={(e) => set("auth_header", e.target.value)} />
            </label>
          </div>
          <label>
            What it is for (optional)
            <input value={form.description} onChange={(e) => set("description", e.target.value)} />
          </label>
          <div className="modal-actions">
            {editing && (
              <button type="button" className="danger" disabled={busy} onClick={() => void remove(editing.name)}>
                Remove
              </button>
            )}
            <span className="spacer" />
            <button type="button" onClick={() => setForm(null)}>
              Cancel
            </button>
            <button type="button" disabled={busy || !form.url || !form.name} onClick={() => void test()}>
              {busy ? "Working…" : "Test"}
            </button>
            <button type="button" className="primary" disabled={busy || !form.url || !form.name} onClick={() => void save()}>
              Save
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="file-button" onClick={() => open(null)}>
          Add an MCP server…
        </button>
      )}
      {result && (
        <p className={result.ok ? "hint ok" : "form-error"}>
          {result.text}
          {result.tools && result.tools.length > 0 && <> Tools: {result.tools.join(", ")}.</>}
        </p>
      )}
    </div>
  );
}

/** On an agent's Permissions tab: which registered MCP servers this agent may use. */
export function McpServerChoice({ chosen, onChange }: { chosen: string[]; onChange: (names: string[]) => void }) {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  useEffect(() => {
    void api
      .listMcpServers()
      .then(setServers)
      .catch(() => setServers([]));
  }, []);
  if (servers === null) return null;
  if (servers.length === 0) {
    return (
      <p className="hint">
        No MCP servers are set up. Add them under <a href="#/settings/mcp">Settings › MCP servers</a>.
      </p>
    );
  }
  return (
    <div className="mcp-choice">
      <span className="hint">MCP servers this agent may use:</span>
      {servers.map((server) => (
        <label key={server.name} className="toggle">
          <input
            type="checkbox"
            checked={chosen.includes(server.name)}
            onChange={(e) =>
              onChange(e.target.checked ? [...chosen, server.name] : chosen.filter((name) => name !== server.name))
            }
          />
          {server.name}
          {server.description && <span className="muted"> · {server.description}</span>}
        </label>
      ))}
    </div>
  );
}
