import { useEffect, useState } from "react";
import { api } from "./api";
import type { ClaudeDesktopServer, McpServer, McpServerForm } from "./types";

const EMPTY: McpServerForm = {
  name: "",
  transport: "stdio",
  url: "",
  auth_header: "Authorization",
  command: "",
  args: [],
  cwd: "",
  description: "",
  always_ask: true,
  token: "",
  env: {},
};

/** Split a typed command line into words, honouring quotes: npx -y "@scope/pkg" "/path with space". */
export function splitCommand(line: string): string[] {
  const words: string[] = [];
  let word = "";
  let quote: string | null = null;
  let started = false;
  for (const char of line) {
    if (quote) {
      if (char === quote) quote = null;
      else word += char;
    } else if (char === '"' || char === "'") {
      quote = char;
      started = true;
    } else if (/\s/.test(char)) {
      if (started || word) words.push(word);
      word = "";
      started = false;
    } else {
      word += char;
    }
  }
  if (started || word) words.push(word);
  return words;
}

function joinCommand(words: string[]): string {
  return words.map((word) => (word === "" || /[\s"']/.test(word) ? `"${word.replace(/"/g, "'")}"` : word)).join(" ");
}

function describe(server: McpServer): string {
  return server.transport === "http" ? server.url : joinCommand([server.command, ...server.args]);
}

/** Environment rows: values the user typed now; stored ones are shown as names with an empty value. */
type EnvRow = { name: string; value: string; stored: boolean };

/** Settings › MCP servers: the one place where MCP servers are set up for every agent. */
export function McpServers() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [form, setForm] = useState<McpServerForm | null>(null);
  const [commandLine, setCommandLine] = useState("");
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string; tools?: string[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [desktop, setDesktop] = useState<ClaudeDesktopServer[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);

  const load = () =>
    api
      .listMcpServers()
      .then(setServers)
      .catch(() => undefined);
  useEffect(() => void load(), []);

  const set = <K extends keyof McpServerForm>(key: K, value: McpServerForm[K]) =>
    setForm((current) => (current ? { ...current, [key]: value } : current));

  function open(server: McpServer | null) {
    setEditing(server);
    setResult(null);
    setDesktop(null);
    setForm(server ? { ...EMPTY, ...server, token: "", env: {} } : EMPTY);
    setCommandLine(server && server.transport === "stdio" ? joinCommand([server.command, ...server.args]) : "");
    setEnvRows((server?.env_keys ?? []).map((name) => ({ name, value: "", stored: true })));
  }

  function payload(): McpServerForm | null {
    if (!form) return null;
    const [command = "", ...args] = splitCommand(commandLine.trim());
    const env: Record<string, string> = {};
    for (const row of envRows) if (row.name.trim()) env[row.name.trim()] = row.value;
    return form.transport === "stdio" ? { ...form, command, args, env } : { ...form, env };
  }

  async function run(what: string, action: () => Promise<void>) {
    setBusy(what);
    setResult(null);
    try {
      await action();
    } catch (err) {
      setResult({ ok: false, text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  const test = () =>
    run("test", async () => {
      const sent = payload();
      if (!sent) return;
      const tested = await api.testMcpServer(sent);
      setResult({ ok: tested.ok, text: tested.detail, tools: tested.tools });
    });

  const save = () =>
    run("save", async () => {
      const sent = payload();
      if (!sent) return;
      await api.saveMcpServer(sent);
      setForm(null);
      setEditing(null);
      await load();
    });

  const remove = (name: string) =>
    run("remove", async () => {
      await api.removeMcpServer(name);
      setForm(null);
      setEditing(null);
      await load();
    });

  const findDesktop = () =>
    run("desktop", async () => {
      const found = (await api.claudeDesktopServers()).servers;
      setDesktop(found);
      setPicked(found.filter((server) => !server.exists).map((server) => server.name));
      if (found.length === 0) setResult({ ok: false, text: "Claude Desktop has no MCP servers set up on this Mac." });
    });

  const importPicked = () =>
    run("import", async () => {
      const imported = await api.importClaudeDesktop(picked);
      setDesktop(null);
      await load();
      setResult({ ok: true, text: `Imported ${imported.map((server) => server.name).join(", ") || "nothing"}.` });
    });

  const canSubmit = !!form?.name && (form.transport === "http" ? !!form.url : !!commandLine.trim());

  return (
    <div className="mcp-servers">
      <p className="hint">
        MCP servers give agents more tools: GitHub, a ticket system, a wiki, a database. Set one up once here, then
        tick it on each agent&apos;s Permissions tab. Like in Claude Desktop, a server is either a <b>command</b>{" "}
        (npx, uvx or a program on this Mac) or a <b>remote</b> address. Tokens and environment values stay in your
        macOS Keychain and never reach an agent&apos;s computer.
      </p>
      {servers.map((server) => (
        <div key={server.name} className="dns-rule">
          <code>{server.name}</code>
          <span className="muted mcp-describe">
            {server.transport === "stdio" ? "Runs on this Mac: " : ""}
            {describe(server)}
            {server.env_keys.length > 0 ? ` · ${server.env_keys.join(", ")}` : ""}
            {server.has_token ? " · token in Keychain" : ""}
            {server.always_ask ? " · always asks" : ""}
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
          <div className="segmented" role="radiogroup" aria-label="Kind of server">
            {(
              [
                ["stdio", "Command"],
                ["http", "Remote (HTTP)"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={form.transport === value}
                disabled={!!editing}
                onClick={() => {
                  set("transport", value);
                  set("always_ask", value === "stdio");
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <label>
            Name
            <input
              value={form.name}
              disabled={!!editing}
              placeholder="e.g. github"
              onChange={(e) => set("name", e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))}
            />
          </label>
          {form.transport === "stdio" ? (
            <>
              <label>
                Command
                <input
                  className="mono"
                  value={commandLine}
                  spellCheck={false}
                  placeholder="npx -y @modelcontextprotocol/server-github"
                  onChange={(e) => setCommandLine(e.target.value)}
                />
              </label>
              <span className="hint">
                As in Claude Desktop: <code>npx -y …</code>, <code>uvx …</code>, or the path of a program. Put
                quotes around words with spaces. Found where your terminal finds it, with your proxy settings.
              </span>
              <div className="mcp-env">
                <span className="field-label">Environment</span>
                {envRows.map((row, index) => (
                  <div key={index} className="row">
                    <input
                      className="mono"
                      value={row.name}
                      placeholder="GITHUB_PERSONAL_ACCESS_TOKEN"
                      spellCheck={false}
                      disabled={row.stored}
                      onChange={(e) =>
                        setEnvRows(envRows.map((r, i) => (i === index ? { ...r, name: e.target.value } : r)))
                      }
                    />
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={row.value}
                      placeholder={row.stored ? "Stored in Keychain (leave empty to keep)" : "value"}
                      onChange={(e) =>
                        setEnvRows(envRows.map((r, i) => (i === index ? { ...r, value: e.target.value } : r)))
                      }
                    />
                    <button
                      type="button"
                      className="link-button"
                      aria-label={`Remove ${row.name || "variable"}`}
                      onClick={() => setEnvRows(envRows.filter((_, i) => i !== index))}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setEnvRows([...envRows, { name: "", value: "", stored: false }])}
                >
                  Add a variable
                </button>
              </div>
              <label>
                Working folder (optional)
                <input className="mono" value={form.cwd} placeholder="~/Code/project" onChange={(e) => set("cwd", e.target.value)} />
              </label>
            </>
          ) : (
            <>
              <label>
                Address
                <input value={form.url} placeholder="https://mcp.example.com/mcp" onChange={(e) => set("url", e.target.value)} />
              </label>
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
            </>
          )}
          <label>
            What it is for (optional)
            <input value={form.description} onChange={(e) => set("description", e.target.value)} />
          </label>
          <label className="toggle">
            <input type="checkbox" checked={form.always_ask} onChange={(e) => set("always_ask", e.target.checked)} />
            Always ask before an agent uses its tools, even where MCP tools are allowed
          </label>
          {form.transport === "stdio" && (
            <p className="hint">
              A command server runs on this Mac with your rights, not inside an agent&apos;s computer: an agent allowed to
              use it can do whatever the server can. Its output goes to <code>~/.config/agent-office/logs</code>.
            </p>
          )}
          <div className="modal-actions">
            {editing && (
              <button type="button" className="danger" disabled={!!busy} onClick={() => void remove(editing.name)}>
                Remove
              </button>
            )}
            <span className="spacer" />
            <button type="button" onClick={() => setForm(null)}>
              Cancel
            </button>
            <button type="button" disabled={!!busy || !canSubmit} onClick={() => void test()}>
              {busy === "test" ? "Testing…" : "Test"}
            </button>
            <button type="button" className="primary" disabled={!!busy || !canSubmit} onClick={() => void save()}>
              Save
            </button>
          </div>
          {busy === "test" && form.transport === "stdio" && (
            <p className="hint">The first start can take a minute while npx or uvx downloads the server.</p>
          )}
        </div>
      ) : (
        <div className="row-actions">
          <button type="button" className="file-button" onClick={() => open(null)}>
            Add an MCP server…
          </button>
          <button type="button" className="file-button" disabled={!!busy} onClick={() => void findDesktop()}>
            {busy === "desktop" ? "Looking…" : "Import from Claude Desktop…"}
          </button>
        </div>
      )}

      {desktop && desktop.length > 0 && (
        <div className="mcp-form">
          <span className="field-label">Servers in Claude Desktop</span>
          {desktop.map((server) => (
            <label key={server.name} className="toggle">
              <input
                type="checkbox"
                checked={picked.includes(server.name)}
                onChange={(e) =>
                  setPicked(e.target.checked ? [...picked, server.name] : picked.filter((n) => n !== server.name))
                }
              />
              <code>{server.name}</code>
              <span className="muted mcp-describe">
                {server.describe}
                {server.env_keys.length > 0 ? ` · ${server.env_keys.join(", ")}` : ""}
                {server.exists ? " · replaces the one set up here" : ""}
              </span>
            </label>
          ))}
          <span className="hint">
            Environment values and tokens go into your Keychain. Command servers are set to always ask.
          </span>
          <div className="modal-actions">
            <span className="spacer" />
            <button type="button" onClick={() => setDesktop(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={!!busy || picked.length === 0}
              onClick={() => void importPicked()}
            >
              Import {picked.length} {picked.length === 1 ? "server" : "servers"}
            </button>
          </div>
        </div>
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
          <span className="muted">
            {server.transport === "stdio" ? " · runs on your Mac with your rights" : ""}
            {server.always_ask ? " · always asks" : ""}
            {server.description ? ` · ${server.description}` : ""}
          </span>
        </label>
      ))}
    </div>
  );
}
