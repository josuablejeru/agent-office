import { useEffect, useState } from "react";
import { api } from "./api";
import type { PackageRepositoryUpdate } from "./types";

const EMPTY: PackageRepositoryUpdate = {
  url: "",
  username: "",
  pypi_repo: "",
  npm_repo: "",
  base_image: "",
  token: "",
};

/** Settings for a company package repository (JFrog Artifactory), used by pip and npm on agents' computers. */
export function PackageRepositorySettings() {
  const [form, setForm] = useState<PackageRepositoryUpdate>(EMPTY);
  const [hasToken, setHasToken] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [imageSource, setImageSource] = useState("");

  useEffect(() => {
    void api
      .packageRepository()
      .then((current) => {
        setForm({ ...EMPTY, ...current, token: "" });
        setHasToken(current.has_token);
        setConfigured(!!current.url);
      })
      .catch(() => undefined);
    void api
      .baseImage()
      .then((status) => setImageSource(status.source))
      .catch(() => undefined);
  }, []);

  const set = (key: keyof PackageRepositoryUpdate, value: string) => setForm((current) => ({ ...current, [key]: value }));

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      const saved = await api.savePackageRepository(form);
      setHasToken(saved.has_token);
      setConfigured(true);
      setForm((current) => ({ ...current, token: "" }));
      setMessage({ ok: true, text: "Saved. Agents\u2019 computers use it from their next start, and so does the next image build." });
    });

  const test = () =>
    run(async () => {
      const result = await api.testPackageRepository(form);
      setMessage({ ok: result.ok, text: result.detail });
    });

  const remove = () =>
    run(async () => {
      await api.removePackageRepository();
      setForm(EMPTY);
      setHasToken(false);
      setConfigured(false);
      setMessage({ ok: true, text: "Removed. Agents use the public package indexes again from their next start." });
    });

  return (
    <div className="package-repository">
      <p className="hint">
        For networks where pip and npm must go through a company JFrog Artifactory. Agents&apos; computers and the
        system image build then fetch packages from it. The token is kept in your macOS Keychain; agents can read it
        on their own computers, so use a read-only token.
      </p>
      <label>
        Artifactory address
        <input
          placeholder="https://artifactory.example.com/artifactory"
          value={form.url}
          onChange={(e) => set("url", e.target.value)}
        />
      </label>
      <div className="row">
        <label>
          Username
          <input value={form.username} autoComplete="off" onChange={(e) => set("username", e.target.value)} />
        </label>
        <label>
          Token
          <input
            type="password"
            autoComplete="new-password"
            placeholder={hasToken ? "Stored in Keychain (leave empty to keep)" : "Access token or API key"}
            value={form.token}
            onChange={(e) => set("token", e.target.value)}
          />
        </label>
      </div>
      <div className="row">
        <label>
          PyPI repository
          <input placeholder="e.g. pypi-remote" value={form.pypi_repo} onChange={(e) => set("pypi_repo", e.target.value)} />
        </label>
        <label>
          npm repository (optional)
          <input placeholder="e.g. npm-remote" value={form.npm_repo} onChange={(e) => set("npm_repo", e.target.value)} />
        </label>
      </div>
      <label>
        Base image for agents&apos; computers (optional)
        <input
          placeholder="repository/path/to/image-arm64.qcow2"
          value={form.base_image}
          onChange={(e) => set("base_image", e.target.value)}
        />
      </label>
      <p className="hint">
        For a company-approved (hardened) image: the next system image build starts from it instead of Debian&apos;s
        public cloud image, and refuses it unless it matches the SHA-256 Artifactory records for it. It must be an
        ARM64 (aarch64) Debian-based image with cloud-init, in qcow2 or raw format.
        {imageSource && (
          <>
            {" "}
            The current system image was built from <strong>{imageSource}</strong>.
          </>
        )}
      </p>
      <div className="modal-actions">
        {configured && (
          <button type="button" className="danger" disabled={busy} onClick={() => void remove()}>
            Remove
          </button>
        )}
        <span className="spacer" />
        <button type="button" disabled={busy || !form.url} onClick={() => void test()}>
          Test
        </button>
        <button type="button" className="primary" disabled={busy || !form.url} onClick={() => void save()}>
          Save
        </button>
      </div>
      {message && <p className={message.ok ? "hint ok" : "form-error"}>{message.text}</p>}
    </div>
  );
}
