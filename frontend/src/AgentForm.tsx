import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Avatar } from "./Avatar";
import { Permissions } from "./Permissions";
import { ModelTest } from "./ModelTest";
import { CUSTOM_AVATAR, PERSONAS, personaFor } from "./personas";
import type { Agent, AgentCreate, Provider } from "./types";

interface Props {
  providers: Provider[];
  /** When set, the form edits this agent instead of creating one. */
  agent?: Agent;
  avatarVersion?: number;
  onAvatarChanged?: () => void;
  onClose: () => void;
  onSaved: (agent: Agent) => Promise<void> | void;
  onDeleted?: () => Promise<void> | void;
  onCleared?: () => void;
  initialTab?: "profile" | "permissions";
}

/** Prefer a model server on this machine: it needs no key and keeps data local. */
function defaultProvider(providers: Provider[]): string {
  const local = providers.find((p) => /\/\/(localhost|127\.0\.0\.1)[:/]/.test(p.base_url ?? ""));
  return (local ?? providers[0])?.name ?? "";
}

function useModels(provider: string | null): string[] {
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    setModels([]);
    if (provider) {
      api
        .listModels(provider)
        .then((list) => !cancelled && setModels(list))
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [provider]);
  return models;
}

export function AgentForm({
  providers,
  agent,
  avatarVersion = 0,
  onAvatarChanged,
  onClose,
  onSaved,
  onDeleted,
  onCleared,
  initialTab = "profile",
}: Props) {
  const [form, setForm] = useState<AgentCreate>({
    name: agent?.name ?? "",
    system_prompt: agent?.system_prompt ?? PERSONAS[0].prompt,
    avatar: agent?.avatar ?? PERSONAS[0].key,
    provider: agent?.provider ?? defaultProvider(providers),
    model: agent?.model ?? "",
    fallback_provider: agent?.fallback_provider ?? null,
    fallback_model: agent?.fallback_model ?? null,
    jev_enabled: agent?.jev_enabled ?? false,
    permissions: agent?.permissions ?? {},
    unknown_action: agent?.unknown_action ?? "default",
    vm_memory_mb: agent?.vm_memory_mb ?? 4096,
    vm_cpus: agent?.vm_cpus ?? 4,
  });
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [tab, setTab] = useState<"profile" | "permissions">(initialTab);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const models = useModels(form.provider);
  const fallbackModels = useModels(form.fallback_provider);

  const set = <K extends keyof AgentCreate>(key: K, value: AgentCreate[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  useEffect(() => {
    if (!photo) {
      setPhotoPreview(null);
      return;
    }
    const url = URL.createObjectURL(photo);
    setPhotoPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [photo]);

  function choosePersona(key: string) {
    const previous = personaFor(form.avatar);
    const untouched = !form.system_prompt.trim() || form.system_prompt === previous.prompt;
    setPhoto(null);
    setForm((current) => ({
      ...current,
      avatar: key,
      // Follow the persona's prompt unless the user has written their own.
      system_prompt: untouched ? personaFor(key).prompt : current.system_prompt,
    }));
  }

  function choosePhoto(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) setPhoto(file);
  }

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form.name || !form.model) {
      // These fields live on the other tab, where the browser cannot point at them.
      setTab("profile");
      setError("Give the agent a name and a model first.");
      return;
    }
    void guarded(async () => {
      const payload = {
        ...form,
        fallback_provider: form.fallback_provider && form.fallback_model ? form.fallback_provider : null,
        fallback_model: form.fallback_provider && form.fallback_model ? form.fallback_model : null,
      };
      let saved: Agent;
      if (agent) {
        const { name: _name, ...changes } = payload;
        saved = await api.updateAgent(agent.id, changes);
      } else {
        saved = await api.createAgent(payload);
      }
      let photoProblem: string | null = null;
      if (photo) {
        try {
          await api.uploadAvatar(saved.id, photo);
          onAvatarChanged?.();
        } catch (err) {
          photoProblem = err instanceof Error ? err.message : String(err);
        }
      }
      await onSaved(saved);
      if (photoProblem) {
        // The agent itself was saved; only the picture was not.
        window.alert(`The agent was saved, but its photo was not: ${photoProblem}`);
      }
    });
  }

  function handleDelete() {
    if (!agent) return;
    const confirmed = window.confirm(
      `Delete "${agent.name}"? This permanently removes its computer, files and browser logins.`,
    );
    if (!confirmed) return;
    void guarded(async () => {
      await api.deleteAgent(agent.id);
      await onDeleted?.();
    });
  }

  function handleClear() {
    if (!agent) return;
    if (!window.confirm(`Clear the conversation with "${agent.name}"? Its computer and files stay.`)) return;
    void guarded(async () => {
      await api.clearConversation(agent.id);
      onCleared?.();
    });
  }

  const usingPhoto = photo !== null || (form.avatar === CUSTOM_AVATAR && !!agent);

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal wide" onSubmit={handleSubmit}>
        <h2>{agent ? `Settings for ${agent.name}` : "New agent"}</h2>
        <div className="tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === "profile"} onClick={() => setTab("profile")}>
            Profile
          </button>
          <button type="button" role="tab" aria-selected={tab === "permissions"} onClick={() => setTab("permissions")}>
            Permissions
          </button>
        </div>

        {tab === "permissions" && (
          <Permissions
            levels={form.permissions}
            unknownAction={form.unknown_action}
            jevEnabled={form.jev_enabled}
            onLevel={(group, level) =>
              setForm((current) => ({ ...current, permissions: { ...current.permissions, [group]: level } }))
            }
            onUnknownAction={(value) => set("unknown_action", value)}
            onJev={(enabled) => set("jev_enabled", enabled)}
          />
        )}

        {tab === "profile" && (
        <>
        <div className="persona-grid" role="radiogroup" aria-label="Character">
          {PERSONAS.map((persona) => (
            <button
              type="button"
              key={persona.key}
              role="radio"
              aria-checked={!usingPhoto && form.avatar === persona.key}
              className={!usingPhoto && form.avatar === persona.key ? "persona active" : "persona"}
              title={persona.blurb}
              onClick={() => choosePersona(persona.key)}
            >
              <Avatar avatar={persona.key} size={44} />
              <span>{persona.title}</span>
            </button>
          ))}
          <button
            type="button"
            className={usingPhoto ? "persona active" : "persona"}
            title="Use a picture of your own"
            onClick={() => fileInput.current?.click()}
          >
            {photoPreview ? (
              <span className="avatar" style={{ width: 44, height: 44 }}>
                <img src={photoPreview} alt="" />
              </span>
            ) : agent && form.avatar === CUSTOM_AVATAR ? (
              <Avatar avatar={CUSTOM_AVATAR} agentId={agent.id} size={44} version={avatarVersion} />
            ) : (
              <span className="avatar upload" style={{ width: 44, height: 44 }}>
                ＋
              </span>
            )}
            <span>Your photo</span>
          </button>
          <input
            ref={fileInput}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            hidden
            onChange={choosePhoto}
          />
        </div>

        <label>
          Name
          <input
            required
            disabled={!!agent}
            pattern="[a-z0-9][a-z0-9\-]{0,62}"
            title="Lowercase letters, digits and dashes"
            placeholder="e.g. research-desk"
            value={form.name}
            onChange={(e) => set("name", e.target.value.toLowerCase())}
          />
        </label>
        <div className="row">
          <label>
            Provider
            <select required value={form.provider} onChange={(e) => set("provider", e.target.value)}>
              {providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Model
            <input
              required
              list="model-options"
              placeholder="Model ID as the provider names it"
              value={form.model}
              onChange={(e) => set("model", e.target.value)}
            />
            <datalist id="model-options">
              {models.map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
          </label>
        </div>
        <ModelTest provider={form.provider} model={form.model} />
        <label>
          Job description
          <textarea
            rows={4}
            placeholder="Who is this coworker and what are they good at?"
            value={form.system_prompt}
            onChange={(e) => set("system_prompt", e.target.value)}
          />
        </label>

        <button type="button" className="disclosure" onClick={() => setAdvanced((open) => !open)}>
          {advanced ? "▾" : "▸"} More options
        </button>
        {advanced && (
          <>
            <div className="row">
              <label>
                Fallback provider
                <select
                  value={form.fallback_provider ?? ""}
                  onChange={(e) => set("fallback_provider", e.target.value || null)}
                >
                  <option value="">None</option>
                  {providers.map((provider) => (
                    <option key={provider.name} value={provider.name}>
                      {provider.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Fallback model
                <input
                  list="fallback-model-options"
                  disabled={!form.fallback_provider}
                  placeholder="Used when the main model fails"
                  value={form.fallback_model ?? ""}
                  onChange={(e) => set("fallback_model", e.target.value || null)}
                />
                <datalist id="fallback-model-options">
                  {fallbackModels.map((model) => (
                    <option key={model} value={model} />
                  ))}
                </datalist>
              </label>
            </div>
            <div className="row">
              <label>
                Memory (MB)
                <input
                  type="number"
                  min={2048}
                  step={512}
                  value={form.vm_memory_mb}
                  onChange={(e) => set("vm_memory_mb", Number(e.target.value))}
                />
              </label>
              <label>
                CPUs
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={form.vm_cpus}
                  onChange={(e) => set("vm_cpus", Number(e.target.value))}
                />
              </label>
            </div>
            {agent && <p className="hint">Memory and CPU changes apply the next time the computer starts.</p>}
          </>
        )}

        </>
        )}

        {error && <p className="form-error">{error}</p>}
        <div className="modal-actions">
          {agent && (
            <>
              <button
                type="button"
                className="danger"
                disabled={busy || agent.vm_status === "running"}
                title={agent.vm_status === "running" ? "Stop the computer first" : undefined}
                onClick={handleDelete}
              >
                Delete agent
              </button>
              <button type="button" disabled={busy} onClick={handleClear}>
                Clear conversation
              </button>
            </>
          )}
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy}>
            {agent ? "Save" : "Hire"}
          </button>
        </div>
      </form>
    </div>
  );
}
