import { useEffect, useState } from "react";
import { api } from "./api";
import { modelName, refreshModelSetup, useModelSetup } from "./modelSetup";
import { ModelTest } from "./ModelTest";
import type { Agent, ModelChoice, Provider, RoutingSettings } from "./types";

/** Settings › Model providers: the models you switch between, under short names. */
export function ModelList({ providers }: { providers: Provider[] }) {
  const [rows, setRows] = useState<ModelChoice[] | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  // Model IDs each provider offers, as suggestions (not every provider can list them).
  const [offered, setOffered] = useState<Record<string, string[]>>({});
  useEffect(() => {
    for (const provider of providers) {
      void api
        .listModels(provider.name)
        .then((ids) => setOffered((current) => ({ ...current, [provider.name]: ids })))
        .catch(() => undefined);
    }
  }, [providers]);

  useEffect(() => {
    void api
      .modelChoices()
      .then((list) => {
        setRows(list);
        setSaved(JSON.stringify(list));
      })
      .catch(() => setRows([]));
  }, []);

  if (rows === null) return null;
  const changed = JSON.stringify(rows) !== saved;
  const edit = (index: number, change: Partial<ModelChoice>) =>
    setRows(rows.map((row, i) => (i === index ? { ...row, ...change } : row)));

  async function save() {
    if (!rows) return;
    setMessage(null);
    try {
      const stored = await api.saveModelChoices(rows.filter((row) => row.label.trim() || row.model.trim()));
      setRows(stored);
      setSaved(JSON.stringify(stored));
      void refreshModelSetup();
      setMessage({ ok: true, text: "Saved. Choose them next to an agent's message box." });
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : String(err) });
    }
  }

  return (
    <div className="model-list">
      <h3>Models to switch between</h3>
      <p className="hint">
        Give the models you use a short name, like <b>Haiku</b> for quick jobs and <b>Opus</b> or <b>Gemini</b> for
        hard ones. Next to an agent&apos;s message box you can then switch the task you send to one of them; the
        agent&apos;s own model stays its default.
      </p>
      {rows.map((row, index) => (
        <div key={index} className="model-row">
          <input
            aria-label="Name"
            placeholder="Name, e.g. Opus"
            value={row.label}
            onChange={(e) => edit(index, { label: e.target.value })}
          />
          <select aria-label="Provider" value={row.provider} onChange={(e) => edit(index, { provider: e.target.value })}>
            {providers.map((provider) => (
              <option key={provider.name} value={provider.name}>
                {provider.name}
              </option>
            ))}
          </select>
          <input
            aria-label="Model ID"
            className="mono"
            placeholder="Model ID as the provider names it"
            list={`model-ids-${row.provider}`}
            value={row.model}
            spellCheck={false}
            onChange={(e) => edit(index, { model: e.target.value })}
          />
          <ModelTest provider={row.provider} model={row.model} />
          <button
            type="button"
            className="link-button"
            aria-label={`Remove ${row.label || "model"}`}
            onClick={() => setRows(rows.filter((_, i) => i !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      {Object.entries(offered).map(([provider, ids]) => (
        <datalist key={provider} id={`model-ids-${provider}`}>
          {ids.map((id) => (
            <option key={id} value={id} />
          ))}
        </datalist>
      ))}
      <div className="row-actions">
        <button
          type="button"
          className="file-button"
          disabled={providers.length === 0}
          onClick={() => setRows([...rows, { label: "", provider: providers[0]?.name ?? "", model: "" }])}
        >
          Add a model
        </button>
        {changed && (
          <button type="button" className="primary" onClick={() => void save()}>
            Save models
          </button>
        )}
      </div>
      {message && <p className={message.ok ? "hint ok" : "form-error"}>{message.text}</p>}
      <Routing models={JSON.parse(saved || "[]") as ModelChoice[]} />
    </div>
  );
}

const NO_ROUTING: RoutingSettings = { router: "", routes: [], escalate_to: "", escalate_after: 3 };

/** Automatic model choice: routes in plain words, the model that picks one, and escalation. */
function Routing({ models }: { models: ModelChoice[] }) {
  const [routing, setRouting] = useState<RoutingSettings | null>(null);
  const [saved, setSaved] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    void api
      .routing()
      .then((loaded) => {
        setRouting(loaded);
        setSaved(JSON.stringify(loaded));
      })
      .catch(() => setRouting(NO_ROUTING));
  }, []);
  if (routing === null) return null;
  if (models.length < 2) {
    return (
      <p className="hint">
        Add at least two models above to let agents choose between them automatically.
      </p>
    );
  }
  const set = (change: Partial<RoutingSettings>) => setRouting({ ...routing, ...change });
  const editRoute = (index: number, change: Partial<{ when: string; use: string }>) =>
    set({ routes: routing.routes.map((route, i) => (i === index ? { ...route, ...change } : route)) });
  const pick = (value: string, onChange: (label: string) => void, label: string, empty?: string) => (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
      {empty !== undefined && <option value="">{empty}</option>}
      {empty === undefined && !value && <option value="">Choose…</option>}
      {models.map((model) => (
        <option key={model.label} value={model.label}>
          {model.label}
        </option>
      ))}
    </select>
  );

  async function save() {
    if (!routing) return;
    setMessage(null);
    try {
      const cleaned = { ...routing, routes: routing.routes.filter((route) => route.when.trim()) };
      const stored = await api.saveRouting(cleaned);
      setRouting(stored);
      setSaved(JSON.stringify(stored));
      void refreshModelSetup();
      setMessage({ ok: true, text: "Saved. Set agents to Auto in their settings, or choose Auto next to the message box." });
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : String(err) });
    }
  }

  return (
    <div className="model-list">
      <h3>Automatic model choice</h3>
      <p className="hint">
        Describe in plain words which kind of task goes to which model. With <b>Auto</b> chosen next to the message
        box, a small, fast model reads your request and picks the route, once per task. It sees only what you
        wrote, never web pages. When it is unsure, the agent&apos;s own model is used.
      </p>
      <label className="inline-field">
        The model that chooses {pick(routing.router, (router) => set({ router }), "The model that chooses")}
      </label>
      {routing.routes.map((route, index) => (
        <div key={index} className="route-row">
          <input
            aria-label={`Route ${index + 1}`}
            placeholder="e.g. Quick questions, lookups and short replies"
            value={route.when}
            onChange={(e) => editRoute(index, { when: e.target.value })}
          />
          <span className="muted">→</span>
          {pick(route.use, (use) => editRoute(index, { use }), `Model for route ${index + 1}`)}
          <button
            type="button"
            className="link-button"
            onClick={() => set({ routes: routing.routes.filter((_, i) => i !== index) })}
          >
            Remove
          </button>
        </div>
      ))}
      <div className="row-actions">
        <button
          type="button"
          className="file-button"
          onClick={() => set({ routes: [...routing.routes, { when: "", use: models[0].label }] })}
        >
          Add a route
        </button>
      </div>
      <label className="inline-field">
        When it gets stuck, hand over to{" "}
        {pick(routing.escalate_to, (escalate_to) => set({ escalate_to }), "Escalate to", "Nobody (no escalation)")}
        {routing.escalate_to && (
          <>
            {" "}after
            <input
              type="number"
              min={2}
              max={10}
              aria-label="Failed actions before handing over"
              value={routing.escalate_after}
              onChange={(e) => set({ escalate_after: Number(e.target.value) || 3 })}
            />
            failed actions in a row, or when the model asks for it
          </>
        )}
      </label>
      {JSON.stringify(routing) !== saved && (
        <div className="row-actions">
          <button type="button" className="primary" onClick={() => void save()}>
            Save automatic choice
          </button>
        </div>
      )}
      {message && <p className={message.ok ? "hint ok" : "form-error"}>{message.text}</p>}
    </div>
  );
}

/** Where the choice for an agent is remembered (in this browser: a convenience, not a setting). */
const storageKey = (agentId: number) => `agent-office.model.${agentId}`;

export function rememberedModel(agentId: number): string {
  try {
    return localStorage.getItem(storageKey(agentId)) ?? "";
  } catch {
    return "";
  }
}

export function rememberModel(agentId: number, label: string) {
  try {
    if (label) localStorage.setItem(storageKey(agentId), label);
    else localStorage.removeItem(storageKey(agentId));
  } catch {
    // Not remembered: the picker starts at the agent's own model next time.
  }
}

export { modelName };


/** At the top of Settings › Models & routing: which agent uses what, and what Auto does. */
export function ModelOverview() {
  const setup = useModelSetup();
  const [agents, setAgents] = useState<Agent[]>([]);
  useEffect(() => {
    void api
      .listAgents()
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);
  const routing = setup.routing;
  return (
    <div className="model-overview">
      <p className="hint">
        {setup.autoReady && routing
          ? `Auto is on: ${routing.router} picks one of ${routing.routes.length} route${routing.routes.length === 1 ? "" : "s"} per task` +
            (routing.escalate_to ? `, and stuck tasks go to ${routing.escalate_to}.` : ".")
          : "Auto is not set up yet: name your models and describe routes below."}{" "}
        Each agent uses its own model or Auto (set in its settings); the switch next to the message box overrides that
        for the tasks you send.
      </p>
      {agents.length > 0 && (
        <table className="model-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Its own model</th>
              <th>Tasks use</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id}>
                <td>
                  <a href={`#/agents/${agent.id}`}>{agent.name}</a>
                </td>
                <td title={`${agent.provider} · ${agent.model}`}>{modelName(setup.models, agent.provider, agent.model)}</td>
                <td>{agent.auto_model ? (setup.autoReady ? "Auto" : "Auto (not set up: its own model)") : "Its own model"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


/** Settings › Models & routing: the on/off switch, and while on, everything it covers. */
export function ModelSwitchingSettings({ providers }: { providers: Provider[] }) {
  const setup = useModelSetup();
  const [error, setError] = useState<string | null>(null);

  async function toggle(enabled: boolean) {
    setError(null);
    try {
      await api.setModelSwitching(enabled);
      await refreshModelSetup();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <label className="toggle switching-toggle">
        <input type="checkbox" checked={setup.enabled} onChange={(e) => void toggle(e.target.checked)} />
        Let agents use different models
      </label>
      {error && <p className="form-error">{error}</p>}
      {setup.enabled ? (
        <>
          <p className="hint">
            On: pick a model per task next to the message box, let <b>Auto</b> choose by your routes, and hand stuck
            tasks to a stronger model.
          </p>
          <ModelOverview />
          <ModelList providers={providers} />
        </>
      ) : (
        <p className="hint">
          Off: every task uses its agent&apos;s own model, set in the agent&apos;s settings. No model switch next to the
          message box, no Auto, no hand-overs. Switch it on to name the models you use and let tasks go to the right
          one: quick jobs to a fast model, hard ones to a strong one.
        </p>
      )}
    </>
  );
}
