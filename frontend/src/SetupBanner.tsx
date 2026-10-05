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
  return (
    <div className="notice setup">
      <div>
        <strong>One-time setup.</strong> Agents need a system image for their computers (Debian with
        a desktop and Chrome). Building it downloads about 1 GB and takes several minutes.
        {status.detail && <div className="setup-detail">{status.detail}</div>}
      </div>
      <button
        className="primary"
        disabled={status.building}
        onClick={() => void api.buildBaseImage().then(setStatus)}
      >
        {status.building ? "Building…" : "Build now"}
      </button>
    </div>
  );
}
