import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api";
import { Avatar } from "./Avatar";
import type { Agent, Channel, ChannelMessage } from "./types";

const POLL_MS = 2000;
const MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
};

interface Props {
  channel: Channel;
  agents: Agent[];
  avatarVersion: number;
}

/** A shared room: you and the agents post here, and @name asks an agent to act. */
export function ChannelView({ channel, agents, avatarVersion }: Props) {
  const [messages, setMessages] = useState<ChannelMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const lastId = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const fresh = await api.channelMessages(channel.id, lastId.current);
        if (cancelled || fresh.length === 0) return;
        lastId.current = fresh[fresh.length - 1].id;
        setMessages((current) => [...current, ...fresh.filter((m) => !current.some((c) => c.id === m.id))]);
      } catch {
        // Keep polling: the backend may be restarting.
      }
    };
    void poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [channel.id]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  async function send() {
    const content = draft.trim();
    if (!content) return;
    setError(null);
    try {
      await api.postToChannel(channel.id, content);
      setDraft("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  function handleKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  }

  function mention(name: string) {
    setDraft((current) => `${current}${current && !current.endsWith(" ") ? " " : ""}@${name} `);
    input.current?.focus();
  }

  const byName = new Map(agents.map((agent) => [agent.name, agent]));

  return (
    <div className="agent-view">
      <header className="agent-header">
        <div className="agent-title">
          <h1>
            <span className="hash">#</span> {channel.name}
          </h1>
          <span className="model-chip">Shared with all agents</span>
        </div>
      </header>
      <section className="chat">
        <div className="messages">
          {messages.length === 0 && (
            <div className="chat-empty">
              <h2>The office channel</h2>
              <p>
                Everyone in the office can read this. Write <strong>@name</strong> to ask an agent to
                act; it answers here, and agents can hand work to each other the same way. An agent
                needs its computer running to respond.
              </p>
            </div>
          )}
          {messages.map((message) => {
            const agent = byName.get(message.author);
            if (message.author === "system") {
              return (
                <div key={message.id} className="channel-note">
                  {message.content}
                </div>
              );
            }
            return (
              <div key={message.id} className="channel-message">
                {agent ? (
                  <Avatar avatar={agent.avatar} agentId={agent.id} size={34} version={avatarVersion} />
                ) : (
                  <span className="avatar you" style={{ width: 34, height: 34 }}>
                    {message.author === "you" ? "You" : message.author.slice(0, 2)}
                  </span>
                )}
                <div className="channel-body">
                  <div className="channel-author">
                    {message.author === "you" ? "You" : message.author}
                    <span className="muted">
                      {new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                    </span>
                  </div>
                  <div className="message assistant">
                    <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
                      {message.content}
                    </Markdown>
                  </div>
                </div>
              </div>
            );
          })}
          <div ref={bottom} />
        </div>

        {error && <div className="notice error">{error}</div>}
        {agents.length > 0 && (
          <div className="mention-row">
            {agents.map((agent) => (
              <button
                key={agent.id}
                className="mention-chip"
                title={agent.vm_status === "running" ? "Mention" : "Offline: start its computer first"}
                onClick={() => mention(agent.name)}
              >
                <span className={`dot ${agent.vm_status}`} aria-hidden /> @{agent.name}
              </button>
            ))}
          </div>
        )}
        <form className="composer" onSubmit={handleSubmit}>
          <textarea
            ref={input}
            rows={1}
            placeholder={`Message #${channel.name}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKey}
          />
          <button type="submit" className="send" aria-label="Send" disabled={!draft.trim()}>
            ↑
          </button>
        </form>
        <div className="composer-foot">
          Agents see this channel and may act on what is written here.
        </div>
      </section>
    </div>
  );
}
