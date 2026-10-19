import { useEffect, useState } from "react";
import { api } from "./api";
import type { BaseImageStatus } from "./types";

const POLL_MS = 3000;

/** First-run setup: offers to build the system image agent computers start from. */
export function SetupBanner() {
  const [status, setStatus] = useState<BaseImageStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    const check = () =>
      api
        .baseImage()
        .then((next) => !cancelled && setStatus(next))
        .catch(() => undefined);
    void check();
    const timer = window.setInterval(check, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (!status || status.ready) return null;

  async function useImageFile() {
    // In the app window, the native file picker; in a browser, the path typed in.
    const native = (window as unknown as { pywebview?: { api?: { choose_image?: () => Promise<string | null> } } })
      .pywebview?.api?.choose_image;
    const path = native
      ? await native()
      : window.prompt("Path to a system image file (debian-desktop.qcow2) built on another Mac:");
    if (path) setStatus(await api.importBaseImage(path));
  }

  const failed = status.detail.startsWith("Build failed") || status.detail.startsWith("Import failed");
  return (
    <div className="notice setup">
      <div>
        <strong>One-time setup.</strong> Agents need a system image for their computers (Debian with
        a desktop and Chrome). Building it downloads about 1 GB and takes several minutes.
        {status.detail && <div className="setup-detail">{status.detail}</div>}
        {failed && (
          <div className="setup-detail">
            If building does not work on this network, build the image on another Mac and use that file
            here: it is <code>~/.config/agent-office/images/debian-desktop.qcow2</code> there.
          </div>
        )}
      </div>
      <div className="setup-actions">
        <button disabled={status.building} onClick={() => void useImageFile()}>
          Use an image file…
        </button>
        <button
          className="primary"
          disabled={status.building}
          onClick={() => void api.buildBaseImage().then(setStatus)}
        >
          {status.building ? (status.detail.startsWith("Copying") ? "Copying…" : "Building…") : "Build now"}
        </button>
      </div>
    </div>
  );
}
