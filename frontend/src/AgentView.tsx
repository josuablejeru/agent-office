import { useEffect, useState } from "react";
import { AgentForm } from "./AgentForm";
import { api } from "./api";
import { Avatar } from "./Avatar";
import { Chat } from "./Chat";
import { Computer } from "./Computer";
import { Files } from "./Files";
import { MemoryPanel } from "./MemoryPanel";
import type { Agent, Provider } from "./types";

const VM_LABELS: Record<Agent["vm_status"], string> = {
  not_created: "Computer not set up",
  stopped: "Computer off",
  running: "Computer on",
};
const MODEL_POLL_MS = 15000;
const GUEST_POLL_MS = 2000;

interface Props {
  agent: Agent;
  providers: Provider[];
  avatarVersion: number;
  onAvatarChanged: () => void;
  onChanged: () => Promise<void>;
}

export function AgentView({ agent, providers, avatarVersion, onAvatarChanged, onChanged }: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [guestReady, setGuestReady] = useState(false);
  const [vmProblem, setVmProblem] = useState<string | null>(null);
  const [modelProblem, setModelProblem] = useState<string | null>(null);
  // Which side panel is open next to the chat.
  const [panel, setPanel] = useState<"computer" | "files" | "memory" | null>(null);
  const [settingsTab, setSettingsTab] = useState<"profile" | "permissions" | null>(null);
  // Changed to make the chat start afresh, after the conversation was cleared.
  const [chatEpoch, setChatEpoch] = useState(0);
  const running = agent.vm_status === "running";

  useEffect(() => {
    if (!running) {
      setGuestReady(false);
      setVmProblem(null);
      return;
    }
    let cancelled = false;
    const check = () =>
      api
        .guestStatus(agent.id)
        .then((status) => {
          if (cancelled) return;
          setGuestReady(status.ready);
          setVmProblem(status.problem);
        })
        .catch(() => undefined);
    void check();
    const timer = window.setInterval(check, GUEST_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [agent.id, running]);

  // Tell the user before they type that the model cannot be reached.
  useEffect(() => {
    let cancelled = false;
    const check = () =>
      api
        .modelStatus(agent.id)
        .then((status) => !cancelled && setModelProblem(status.ok ? null : status.detail))
        .catch(() => undefined);
    void check();
    const timer = window.setInterval(check, MODEL_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [agent.id, agent.provider, agent.model]);

  async function vmAction(label: string, action: (id: number) => Promise<Agent>) {
    setPending(label);
    setError(null);
    try {
      await action(agent.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
      await onChanged();
    }
  }

  const status = pending ?? (running && !guestReady ? "Starting up…" : VM_LABELS[agent.vm_status]);

  return (
    <div className="agent-view">
      <header className="agent-header">
        <div className="agent-title">
          <Avatar avatar={agent.avatar} agentId={agent.id} size={34} version={avatarVersion} />
          <h1>{agent.name}</h1>
          <span className="model-chip" title="Model used for this agent">
            {agent.model} · {agent.provider}
          </span>
          <span className={`status-chip ${running && guestReady ? "on" : ""}`}>{status}</span>
        </div>
        <div className="agent-actions">
          {running ? (
            <button disabled={!!pending} onClick={() => void vmAction("Stopping…", api.stopVm)}>
              Turn off
            </button>
          ) : (
            <button
              className="primary"
              disabled={!!pending}
              onClick={() => void vmAction("Starting…", api.startVm)}
            >
              Turn on computer
            </button>
          )}
          <button disabled={!running} onClick={() => setPanel(panel === "computer" ? null : "computer")}>
            {panel === "computer" ? "Close computer" : "Open computer"}
          </button>
          <button disabled={!running} onClick={() => setPanel(panel === "files" ? null : "files")}>
            {panel === "files" ? "Close files" : "Files"}
          </button>
          <button disabled={!running} onClick={() => setPanel(panel === "memory" ? null : "memory")}>
            {panel === "memory" ? "Close memory" : "Memory"}
          </button>
          <button onClick={() => setSettingsTab("permissions")}>Permissions</button>
          <button onClick={() => setSettingsTab("profile")}>Agent settings</button>
        </div>
      </header>
      {error && <div className="notice error">{error}</div>}
      {vmProblem && <div className="notice error">{vmProblem}</div>}
      {modelProblem && <div className="notice">{modelProblem}</div>}

      <div className={panel && running ? "workspace split" : "workspace"}>
        <Chat key={chatEpoch} agent={agent} ready={running && guestReady} />
        {panel === "computer" && running && <Computer agent={agent} />}
        {panel === "files" && running && <Files agent={agent} />}
        {panel === "memory" && running && <MemoryPanel agent={agent} />}
      </div>

      {settingsTab && (
        <AgentForm
          initialTab={settingsTab}
          agent={agent}
          providers={providers}
          avatarVersion={avatarVersion}
          onAvatarChanged={onAvatarChanged}
          onClose={() => setSettingsTab(null)}
          onSaved={async () => {
            setSettingsTab(null);
            await onChanged();
          }}
          onCleared={() => {
            setSettingsTab(null);
            setChatEpoch((epoch) => epoch + 1);
          }}
          onDeleted={async () => {
            setSettingsTab(null);
            await onChanged();
          }}
        />
      )}
    </div>
  );
}
