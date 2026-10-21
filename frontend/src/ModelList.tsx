import { useEffect, useState } from "react";
import { api } from "./api";
import { ModelTest } from "./ModelTest";
import type { ModelChoice, Provider } from "./types";

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

/** The name a run's model goes by in the list, or its ID. */
export function modelName(models: ModelChoice[], provider: string, model: string): string {
  return models.find((choice) => choice.provider === provider && choice.model === model)?.label ?? model;
}
