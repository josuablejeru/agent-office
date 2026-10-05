import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Agent, SharedFile } from "./types";

const REFRESH_MS = 4000;

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/** The agent's Shared folder: send it files, and collect what it made for you. */
export function Files({ agent }: { agent: Agent }) {
  const [files, setFiles] = useState<SharedFile[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error`: a background refresh must not erase what just went wrong.
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setFiles(await api.listFiles(agent.id));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [agent.id]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const chosen = [...(event.target.files ?? [])];
    event.target.value = "";
    if (chosen.length === 0) return;
    setBusy(true);
    setNotice(null);
    setActionError(null);
    try {
      for (const file of chosen) await api.uploadFile(agent.id, file);
      setNotice(
        chosen.length === 1 ? `Sent ${chosen[0].name} to ~/Shared.` : `Sent ${chosen.length} files to ~/Shared.`,
      );
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function save(file: SharedFile) {
    setBusy(true);
    setNotice(null);
    try {
      const { saved_to } = await api.saveFile(agent.id, file.path);
      setNotice(`Saved to ${saved_to.replace(/^\/Users\/[^/]+/, "~")}`);
      setActionError(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="computer files">
      <div className="computer-bar">
        <span className="computer-title">
          {agent.name}&apos;s shared folder <span className="muted">· ~/Shared on its computer</span>
        </span>
        <button className="primary" disabled={busy} onClick={() => picker.current?.click()}>
          Send a file
        </button>
        <input ref={picker} type="file" multiple hidden onChange={(e) => void upload(e)} />
      </div>
      {error && <div className="notice error">{error}</div>}
      {actionError && (
        <div className="notice error" onClick={() => setActionError(null)} title="Dismiss">
          {actionError}
        </div>
      )}
      {notice && <div className="notice ok">{notice}</div>}
      <div className="file-list">
        {files?.length === 0 && (
          <p className="hint">
            Nothing here yet. Send a file for the agent to work on, or ask it to save its results in
            ~/Shared. Files can be up to 25 MB.
          </p>
        )}
        {files?.map((file) => (
          <div key={file.path} className="file-row">
            <span className="file-name" title={file.path}>
              {file.path}
            </span>
            <span className="muted">{formatSize(file.size)}</span>
            <button disabled={busy} onClick={() => void save(file)}>
              Save to Downloads
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
