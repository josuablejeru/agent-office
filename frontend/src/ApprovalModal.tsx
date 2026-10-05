import { useState } from "react";
import { api } from "./api";
import type { PendingApproval } from "./types";

const CONTENT_PREVIEW_CHARS = 2000;

/** Everything the user needs to judge the action: for a file write, that includes the content. */
function describe(approval: PendingApproval): string {
  const { command, path, url, content } = approval.arguments;
  if (typeof command === "string") return command;
  if (typeof path === "string" && typeof content === "string") {
    const more = content.length > CONTENT_PREVIEW_CHARS;
    const preview = content.slice(0, CONTENT_PREVIEW_CHARS);
    const tail = more ? `\n… (${content.length - CONTENT_PREVIEW_CHARS} more characters)` : "";
    return `Write to ${path}:\n\n${preview}${tail}`;
  }
  const subject = path ?? url;
  return typeof subject === "string" ? subject : JSON.stringify(approval.arguments, null, 2);
}

interface Props {
  agentName: string;
  approval: PendingApproval;
  onAnswered: () => void;
}

export function ApprovalModal({ agentName, approval, onAnswered }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function answer(approved: boolean) {
    setBusy(true);
    try {
      await api.answerApproval(approval.id, approved);
      onAnswered();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const risk = (approval.risk ?? "medium").toLowerCase();
  return (
    <div className="modal-backdrop">
      <div className="modal approval" role="alertdialog" aria-label="Approval required">
        <h2>{agentName} wants to run</h2>
        <pre className="approval-command">{describe(approval)}</pre>
        <dl>
          <dt>Tool</dt>
          <dd>{approval.tool}</dd>
          <dt>Risk</dt>
          <dd className={`risk ${risk}`}>{risk.charAt(0).toUpperCase() + risk.slice(1)}</dd>
          <dt>Reason</dt>
          <dd>{approval.reason || "This action needs your approval."}</dd>
        </dl>
        {error && <p className="form-error">{error}</p>}
        <div className="modal-actions">
          <span className="spacer" />
          <button disabled={busy} onClick={() => void answer(false)}>
            Reject
          </button>
          <button className="primary" disabled={busy} onClick={() => void answer(true)}>
            Allow once
          </button>
        </div>
      </div>
    </div>
  );
}
