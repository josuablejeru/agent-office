import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { BaseImageStatus, ComputerState } from "./types";

/** Rebuilding the system image, and moving agents' computers onto it. */
export function SystemImageSettings() {
  const [image, setImage] = useState<BaseImageStatus | null>(null);
  const [computers, setComputers] = useState<ComputerState[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<number[] | "all" | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [status, list] = await Promise.all([api.baseImage(), api.computers()]);
      setImage(status);
      setComputers(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const updating = computers.some((c) => c.update.state === "queued" || c.update.state === "running");
  const busy = !!image?.building || updating;

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [busy, refresh]);

  async function act(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    await refresh();
  }

  const outdated = computers.filter((c) => c.outdated);

  return (
    <div className="package-repository">
      <p className="hint">
        Every agent&apos;s computer starts from one system image: Debian with a desktop, Chrome and the agent software.
        Rebuilding it picks up Debian&apos;s current updates and the settings under Network and Package repository. A
        rebuild leaves agents&apos; computers as they are; new agents start from the new image, and existing ones move
        to it when you update them.
      </p>
      {image && (
        <p className="hint">
          {image.ready ? (
            <>
              Current image: <strong>{image.version}</strong>
              {image.source && <> — from {image.source}</>}
            </>
          ) : (
            "There is no system image yet."
          )}
        </p>
      )}
      {image?.building && <p className="hint">Building… {image.detail}</p>}
      {image && !image.building && image.detail && <p className="hint">{image.detail}</p>}
      <div className="modal-actions">
        <span className="spacer" />
        <button
          type="button"
          disabled={!image?.ready || busy}
          onClick={() => void act(() => api.rebuildBaseImage())}
          title="Takes about 15 to 30 minutes; agents can keep working meanwhile"
        >
          Rebuild system image
        </button>
      </div>

      <h4>Agents&apos; computers</h4>
      <p className="hint">
        Updating gives an agent a fresh computer on the current image. Its <strong>Shared folder</strong>,{" "}
        <strong>databases</strong> and <strong>memory</strong> come along. Anything else on its computer does not:
        programs it installed, browser sign-ins, files outside the Shared folder and snapshots. Its chats and settings
        are on this Mac and stay. The computer restarts twice; a task it is working on is stopped.
      </p>
      {computers.length === 0 && <p className="hint">No agents yet.</p>}
      <ul className="computer-list">
        {computers.map((c) => (
          <li key={c.agent_id}>
            <span className="name">{c.name}</span>
            <span className="state">
              {c.update.state === "queued" || c.update.state === "running" ? (
                c.update.detail
              ) : c.update.state === "failed" ? (
                <span className="form-error">Update failed: {c.update.detail}</span>
              ) : c.outdated ? (
                "On an older system image"
              ) : c.update.state === "done" ? (
                c.update.detail
              ) : (
                "Up to date"
              )}
            </span>
            {c.outdated && c.update.state !== "queued" && c.update.state !== "running" && (
              <button type="button" disabled={busy} onClick={() => setConfirming([c.agent_id])}>
                Update
              </button>
            )}
          </li>
        ))}
      </ul>
      {outdated.length > 1 && (
        <div className="modal-actions">
          <span className="spacer" />
          <button type="button" disabled={busy} onClick={() => setConfirming("all")}>
            Update all ({outdated.length})
          </button>
        </div>
      )}
      {confirming !== null && (
        <div className="confirm-box">
          <p>
            {confirming === "all"
              ? `Update ${outdated.length} agents' computers, one after another?`
              : `Update ${computers.find((c) => c.agent_id === confirming[0])?.name}'s computer?`}{" "}
            Only the Shared folder, databases and memory are kept.
          </p>
          <div className="modal-actions">
            <span className="spacer" />
            <button type="button" onClick={() => setConfirming(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => {
                const ids = confirming === "all" ? null : confirming;
                setConfirming(null);
                void act(() => api.updateComputers(ids));
              }}
            >
              Update
            </button>
          </div>
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}
