import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { UpdateStatus } from "./types";

const POLL_MS = 1500;
const IDLE_POLL_MS = 60_000;

// Release notes come from GitHub: links open outside, images are not loaded.
const NOTES_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
  img: ({ alt }) => <span className="muted">[image{alt ? `: ${alt}` : ""}]</span>,
};

/** Tells the user a new release exists, and installs it when they say so. */
export function UpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);

  const busy = status?.state === "downloading" || status?.state === "installing";
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .updateStatus()
        .then((next) => !cancelled && setStatus(next))
        .catch(() => undefined);
    void load();
    const timer = window.setInterval(load, busy ? POLL_MS : IDLE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [busy]);

  if (!status || !status.available || status.dismissed === status.latest) return null;
  const act = (action: () => Promise<UpdateStatus>) => void action().then(setStatus).catch(() => undefined);

  return (
    <div className="notice update">
      <div className="update-text">
        <strong>
          Agent Office {status.latest} is available{/-rc\d+$/.test(status.latest) ? " (release candidate)" : ""}.
        </strong>{" "}
        You have {status.current}.
        {status.state === "downloading" && (
          <div className="setup-detail">
            Downloading… {Math.round(status.progress * 100)}%{status.detail && ` · ${status.detail}`}
          </div>
        )}
        {(status.state === "ready" || status.state === "error" || status.state === "installing") && status.detail && (
          <div className="setup-detail">{status.detail}</div>
        )}
        {!status.can_install && status.install_note && <div className="setup-detail">{status.install_note}</div>}
        {status.notes && (
          <button type="button" className="disclosure" onClick={() => setNotesOpen((open) => !open)}>
            {notesOpen ? "▾ Hide what's new" : "▸ What's new"}
          </button>
        )}
        {notesOpen && (
          <div className="update-notes">
            <Markdown remarkPlugins={[remarkGfm]} components={NOTES_COMPONENTS}>
              {status.notes}
            </Markdown>
          </div>
        )}
      </div>
      <div className="setup-actions">
        <button disabled={busy} onClick={() => act(api.dismissUpdate)}>
          Later
        </button>
        {status.can_install ? (
          status.state === "ready" ? (
            <button className="primary" onClick={() => act(api.installUpdate)}>
              Restart to update
            </button>
          ) : (
            <>
              {status.state === "error" && (
                <a
                  className="button-link"
                  href={`https://github.com/josuablejeru/agent-office/releases/tag/${status.latest}`}
                  target="_blank"
                  rel="noreferrer"
                  title="Download it in your browser and install it by hand"
                >
                  Download from GitHub
                </a>
              )}
              <button className="primary" disabled={busy} onClick={() => act(api.downloadUpdate)}>
                {status.state === "downloading"
                  ? "Downloading…"
                  : status.state === "installing"
                    ? "Restarting…"
                    : status.state === "error"
                      ? "Try again"
                      : "Update"}
              </button>
            </>
          )
        ) : (
          <a
            className="button-link primary"
            href={`https://github.com/josuablejeru/agent-office/releases/tag/${status.latest}`}
            target="_blank"
            rel="noreferrer"
          >
            Download
          </a>
        )}
      </div>
    </div>
  );
}
