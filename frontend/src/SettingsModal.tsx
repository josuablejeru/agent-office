import { useEffect, useState } from "react";
import { api } from "./api";
import { ModelTest } from "./ModelTest";
import type { DnsStatus, Provider } from "./types";

interface Props {
  providers: Provider[];
  onClose: () => void;
  onChanged: () => Promise<void>;
}

/** A model field with a Test button, to try a provider without creating an agent. */
function TryModel({ provider }: { provider: Provider }) {
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    void api
      .listModels(provider.name)
      .then(setModels)
      .catch(() => undefined);
  }, [provider.name, provider.has_key, provider.project]);
  return (
    <div className="provider-try">
      <input
        list={`models-${provider.name}`}
        placeholder="Model to test"
        value={model}
        onChange={(e) => setModel(e.target.value)}
      />
      <datalist id={`models-${provider.name}`}>
        {models.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <ModelTest provider={provider.name} model={model} />
    </div>
  );
}

function useSaver(onChanged: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, save };
}

function KeyProvider({ provider, onChanged }: { provider: Provider; onChanged: () => Promise<void> }) {
  const [value, setValue] = useState("");
  const { busy, error, save } = useSaver(onChanged);
  return (
    <div className="provider">
      <div className="provider-head">
        <strong>{provider.name}</strong>
        <span className="muted">{provider.base_url ?? provider.type}</span>
        <span className={provider.key_name && !provider.has_key ? "key-state missing" : "key-state"}>
          {!provider.key_name ? "no key needed" : provider.has_key ? "key set" : "no key"}
        </span>
      </div>
      {provider.key_name && (
        <div className="provider-key">
          <input
            type="password"
            autoComplete="off"
            placeholder={provider.has_key ? "Replace API key" : "Paste API key"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button
            disabled={busy || !value.trim()}
            onClick={() =>
              void save(async () => {
                await api.setProviderKey(provider.name, value.trim());
                setValue("");
              })
            }
          >
            Save
          </button>
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
      <TryModel provider={provider} />
    </div>
  );
}

function GoogleCloudProvider({ provider, onChanged }: { provider: Provider; onChanged: () => Promise<void> }) {
  const [project, setProject] = useState(provider.project ?? "");
  const [region, setRegion] = useState(provider.region ?? "global");
  const { busy, error, save } = useSaver(onChanged);
  const ready = !!provider.project && provider.credentials_found;
  const changed = project !== (provider.project ?? "") || region !== (provider.region ?? "global");
  return (
    <div className="provider">
      <div className="provider-head">
        <strong>{provider.name}</strong>
        <span className="muted">
          {provider.type === "vertex-anthropic" ? "Claude on Vertex AI" : "Gemini and others on Vertex AI"}
        </span>
        <span className={ready ? "key-state" : "key-state missing"}>
          {!provider.project ? "no project" : provider.credentials_found ? "ready" : "not signed in"}
        </span>
      </div>
      <div className="provider-key">
        <input placeholder="Google Cloud project ID" value={project} onChange={(e) => setProject(e.target.value.trim())} />
        <input className="narrow" placeholder="Region" value={region} onChange={(e) => setRegion(e.target.value.trim())} />
        <button
          disabled={busy || !changed || !region}
          onClick={() => void save(() => api.setProviderLocation(provider.name, project, region))}
        >
          Save
        </button>
      </div>
      {!provider.credentials_found && (
        <p className="hint">
          Sign in once in Terminal: <code>gcloud auth application-default login</code>
        </p>
      )}
      {error && <p className="form-error">{error}</p>}
      <TryModel provider={provider} />
    </div>
  );
}

function AddProvider({ onChanged }: { onChanged: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [needsKey, setNeedsKey] = useState(false);
  const { busy, error, save } = useSaver(onChanged);
  return (
    <div className="provider">
      <div className="provider-head">
        <strong>Add a model server</strong>
        <span className="muted">Any OpenAI-compatible endpoint, on this Mac or your network</span>
      </div>
      <div className="provider-key">
        <input className="narrow" placeholder="name" value={name} onChange={(e) => setName(e.target.value.toLowerCase().trim())} />
        <input placeholder="http://192.168.1.50:11434/v1" value={url} onChange={(e) => setUrl(e.target.value.trim())} />
        <button
          disabled={busy || !name || !url}
          onClick={() =>
            void save(async () => {
              await api.addProvider(name, url, needsKey);
              setName("");
              setUrl("");
            })
          }
        >
          Add
        </button>
      </div>
      <label className="toggle">
        <input type="checkbox" checked={needsKey} onChange={(e) => setNeedsKey(e.target.checked)} />
        This server needs an API key
      </label>
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

export function SettingsModal({ providers, onClose, onChanged }: Props) {
  const [keepRunning, setKeepRunning] = useState<boolean | null>(null);
  const [dns, setDns] = useState<DnsStatus | null>(null);

  useEffect(() => {
    void api
      .getSettings()
      .then((settings) => setKeepRunning(settings.keep_vms_running_on_quit))
      .catch(() => undefined);
    void api
      .dnsStatus()
      .then(setDns)
      .catch(() => undefined);
  }, []);

  async function toggleKeepRunning(next: boolean) {
    setKeepRunning(next);
    await api.setSettings({ keep_vms_running_on_quit: next }).catch(() => setKeepRunning(!next));
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <h2>Settings</h2>

        <label className="toggle">
          <input
            type="checkbox"
            checked={keepRunning ?? false}
            disabled={keepRunning === null}
            onChange={(e) => void toggleKeepRunning(e.target.checked)}
          />
          Keep agents&apos; computers running after I quit the app
        </label>
        <p className="hint">
          Off: quitting shuts every agent&apos;s computer down cleanly (nothing is lost). On: they keep
          running in the background and use memory until you stop them.
        </p>

        <h3>Network</h3>
        {dns?.active ? (
          <>
            <p className="hint">
              Agents look up names the way this Mac does, including domains a VPN adds. Read live from
              this Mac{Object.keys(dns.rules).length === 0 && ": no internal domains are set up right now"}.
            </p>
            {Object.entries(dns.rules).map(([domain, servers]) => (
              <div key={domain} className="dns-rule">
                <code>{domain}</code>
                <span className="muted">answered by {servers.join(", ")}</span>
              </div>
            ))}
          </>
        ) : (
          <p className="hint">
            Agents use their own basic name lookup; names that only resolve through a VPN will not
            work on their computers.
          </p>
        )}

        <h3>Model providers</h3>
        <p className="hint">
          API keys are stored in your macOS Keychain and never reach an agent&apos;s computer. More
          providers can be added in <code>~/.config/agent-office/config.yaml</code>.
        </p>
        {providers.map((provider) =>
          provider.uses_google_cloud ? (
            <GoogleCloudProvider key={provider.name} provider={provider} onChanged={onChanged} />
          ) : (
            <KeyProvider key={provider.name} provider={provider} onChanged={onChanged} />
          ),
        )}
        <AddProvider onChanged={onChanged} />
        <div className="modal-actions">
          <span className="spacer" />
          <button onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
