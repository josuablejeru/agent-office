import { useCallback, useEffect, useState } from "react";
import { AgentForm } from "./AgentForm";
import { AgentView } from "./AgentView";
import { api, loadToken, saveToken, setToken, UnauthorizedError } from "./api";
import { Avatar } from "./Avatar";
import { ChannelView } from "./ChannelView";
import { SettingsModal } from "./SettingsModal";
import { SetupBanner } from "./SetupBanner";
import { TokenPrompt } from "./TokenPrompt";
import type { Agent, Capabilities, Channel, Provider } from "./types";

const REFRESH_INTERVAL_MS = 3000;

type View = { kind: "agent"; id: number } | { kind: "channel"; id: number } | null;

const ACTIVITY_LABELS: Record<Agent["activity"], string> = {
  idle: "",
  working: "Working",
  needs_approval: "Needs your approval",
};

export function App() {
  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [view, setView] = useState<View>(null);
  const [creating, setCreating] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Name being typed for a new channel; null while not adding one.
  const [newChannel, setNewChannel] = useState<string | null>(null);
  // Bumped when an avatar photo changes, so every copy of it reloads.
  const [avatarVersion, setAvatarVersion] = useState(0);

  const refresh = useCallback(async () => {
    const [agentList, channelList] = await Promise.all([api.listAgents(), api.listChannels()]);
    setAgents(agentList);
    setChannels(channelList);
    setView((current) => {
      const exists =
        (current?.kind === "agent" && agentList.some((a) => a.id === current.id)) ||
        (current?.kind === "channel" && channelList.some((c) => c.id === current.id));
      if (exists) return current;
      return agentList[0] ? { kind: "agent", id: agentList[0].id } : null;
    });
  }, []);

  const connect = useCallback(
    async (token: string) => {
      setToken(token);
      try {
        const [providerList, caps] = await Promise.all([api.listProviders(), api.capabilities()]);
        await refresh();
        setProviders(providerList);
        setCapabilities(caps);
        saveToken(token);
        setAuthorized(true);
        setError(null);
      } catch (err) {
        if (err instanceof UnauthorizedError) setAuthorized(false);
        else setError(err instanceof Error ? err.message : String(err));
      }
    },
    [refresh],
  );

  useEffect(() => {
    void connect(loadToken());
  }, [connect]);

  // If the first attempt failed for a reason other than the token (the backend
  // was still starting), keep trying: the app window has no reload button.
  useEffect(() => {
    if (authorized !== null) return;
    const timer = window.setInterval(() => void connect(loadToken()), 3000);
    return () => window.clearInterval(timer);
  }, [authorized, connect]);

  // An error belongs to what the user was doing; moving elsewhere dismisses it.
  useEffect(() => {
    setError(null);
  }, [view?.kind, view?.id]);

  useEffect(() => {
    if (!authorized) return;
    const timer = window.setInterval(() => void refresh().catch(() => undefined), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [authorized, refresh]);

  // The same signal as the Dock badge, for when the UI runs in a browser tab.
  const waiting = agents.filter((agent) => agent.activity === "needs_approval").length;
  useEffect(() => {
    document.title = waiting > 0 ? `(${waiting}) Agent Office` : "Agent Office";
  }, [waiting]);

  if (authorized === false) return <TokenPrompt onSubmit={connect} />;

  const selectedAgent = view?.kind === "agent" ? agents.find((a) => a.id === view.id) : undefined;
  const selectedChannel =
    view?.kind === "channel" ? channels.find((c) => c.id === view.id) : undefined;

  async function addChannel(name: string) {
    setNewChannel(null);
    if (!name.trim()) return;
    try {
      const channel = await api.createChannel(name.trim());
      await refresh();
      setView({ kind: "channel", id: channel.id });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">Agent Office</div>
        <button className="new-agent" onClick={() => setCreating(true)}>
          <span aria-hidden>＋</span> New agent
        </button>
        <nav>
          <div className="nav-heading">Agents</div>
          {agents.map((agent) => (
            <button
              key={agent.id}
              className={selectedAgent?.id === agent.id ? "agent-link active" : "agent-link"}
              onClick={() => setView({ kind: "agent", id: agent.id })}
              title={ACTIVITY_LABELS[agent.activity] || undefined}
            >
              <span className="avatar-wrap">
                <Avatar avatar={agent.avatar} agentId={agent.id} size={30} version={avatarVersion} />
                <span className={`dot ${agent.vm_status}`} aria-hidden />
              </span>
              <span className="agent-link-name">{agent.name}</span>
              <span className="agent-link-model">
                {agent.activity === "needs_approval" ? (
                  <span className="badge">Needs approval</span>
                ) : agent.activity === "working" ? (
                  "Working…"
                ) : (
                  agent.model
                )}
              </span>
            </button>
          ))}
          <div className="nav-heading">
            Channels
            <button className="nav-add" title="New channel" onClick={() => setNewChannel("")}>
              ＋
            </button>
          </div>
          {newChannel !== null && (
            <input
              autoFocus
              className="channel-input"
              placeholder="channel-name"
              value={newChannel}
              onChange={(e) => setNewChannel(e.target.value.toLowerCase())}
              onBlur={() => setNewChannel(null)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addChannel(newChannel);
                if (e.key === "Escape") setNewChannel(null);
              }}
            />
          )}
          {channels.map((channel) => (
            <button
              key={channel.id}
              className={selectedChannel?.id === channel.id ? "channel-link active" : "channel-link"}
              onClick={() => setView({ kind: "channel", id: channel.id })}
            >
              <span className="hash">#</span> {channel.name}
            </button>
          ))}
        </nav>
        <button className="sidebar-link" onClick={() => setSettingsOpen(true)}>
          Settings
        </button>
        <div className="sidebar-foot">Local‑first · your keys stay on this Mac</div>
      </aside>

      <main className="main">
        {capabilities && !capabilities.ready && (
          <div className="notice">
            This Mac is not ready to run agent computers: {capabilities.problems.join(" ")}
          </div>
        )}
        {error && <div className="notice error">{error}</div>}
        {authorized && capabilities?.ready && <SetupBanner />}
        {selectedAgent ? (
          <AgentView
            key={selectedAgent.id}
            agent={selectedAgent}
            providers={providers}
            avatarVersion={avatarVersion}
            onAvatarChanged={() => setAvatarVersion((v) => v + 1)}
            onChanged={refresh}
          />
        ) : selectedChannel ? (
          <ChannelView
            key={selectedChannel.id}
            channel={selectedChannel}
            agents={agents}
            avatarVersion={avatarVersion}
          />
        ) : (
          authorized && (
            <div className="empty-state">
              <h1>Hire your first coworker.</h1>
              <p>
                Each agent gets its own persistent computer with a desktop and a browser, a model you
                choose, and a seat in the office channels.
              </p>
              <button className="primary" onClick={() => setCreating(true)}>
                New agent
              </button>
            </div>
          )
        )}
      </main>

      {settingsOpen && (
        <SettingsModal
          providers={providers}
          onClose={() => setSettingsOpen(false)}
          onChanged={async () => setProviders(await api.listProviders())}
        />
      )}

      {creating && (
        <AgentForm
          providers={providers}
          onClose={() => setCreating(false)}
          onSaved={async (agent) => {
            setCreating(false);
            await refresh();
            setView({ kind: "agent", id: agent.id });
          }}
        />
      )}
    </div>
  );
}
