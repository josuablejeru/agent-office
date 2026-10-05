import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api";
import { ApprovalModal } from "./ApprovalModal";
import { Screenshot } from "./Screenshot";
import type { Agent, Message, Run, ToolCall } from "./types";

const RUN_POLL_MS = 1000;
const RUNS_SHOWN = 30;
const OUTCOME_NOTES: Record<string, string> = {
  approved: "approved by you",
  rejected: "rejected by you",
  blocked: "blocked by policy",
  invalid: "invalid arguments",
  cancelled: "cancelled",
};

// Links open in the system browser: followed in place, they would replace the
// app's own window, which has no back button.
// Images are not loaded: fetching one would send its address, which a manipulated
// agent could fill with private text, to an outside server without any click.
const MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
  img: ({ alt }) => <span className="muted">[image{alt ? `: ${alt}` : ""}]</span>,
};

function summarize(call: ToolCall): string {
  const { command, path, url, text, key, ref } = call.arguments;
  const subject = command ?? path ?? url ?? text ?? key ?? (ref !== undefined ? `element ${ref}` : "");
  return typeof subject === "string" ? subject : JSON.stringify(subject);
}

function ToolCalls({ agentId, calls }: { agentId: number; calls: ToolCall[] }) {
  return (
    <div className="tool-calls">
      {calls.map((call) => {
        const pending = call.result === null;
        const failed =
          !pending && (!["allow", "approved"].includes(call.decision ?? "") || "error" in call.result!);
        const screenshot = call.result?.screenshot;
        const state = pending ? "pending" : failed ? "failed" : "";
        return (
          <div key={call.id}>
            <details className={`tool-call ${state}`}>
              <summary>
                <span className="tool-name">{call.tool}</span>
                <code>{summarize(call)}</code>
                {call.decision && OUTCOME_NOTES[call.decision] && (
                  <span className="tool-note">{OUTCOME_NOTES[call.decision]}</span>
                )}
              </summary>
              <pre>{JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2)}</pre>
            </details>
            {typeof screenshot === "string" && <Screenshot agentId={agentId} filename={screenshot} />}
          </div>
        );
      })}
    </div>
  );
}

export function Chat({ agent, ready }: { agent: Agent; ready: boolean }) {
  // A run can start without this chat sending anything: a channel mention, or a
  // colleague handing work over. The agent's activity changing is the signal to look.
  const activity = agent.activity;
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Record<number, Run>>({});
  const [activeRun, setActiveRun] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const list = await api.listMessages(agent.id);
    setMessages(list);
    const runIds = [...new Set(list.map((m) => m.run_id).filter((id): id is number => id !== null))];
    const loaded = await Promise.all(runIds.slice(-RUNS_SHOWN).map((id) => api.getRun(id)));
    setRuns(Object.fromEntries(loaded.map((run) => [run.id, run])));
    const unfinished = loaded.find((run) => run.status === "running");
    if (unfinished) setActiveRun(unfinished.id);
  }, [agent.id]);

  const pollRun = useCallback(
    async (runId: number) => {
      const run = await api.getRun(runId);
      setRuns((current) => ({ ...current, [run.id]: run }));
      if (run.status !== "running") {
        setActiveRun(null);
        await load();
      }
    },
    [load],
  );

  useEffect(() => {
    void load().catch((err) => setError(String(err.message ?? err)));
  }, [load, activity]);

  useEffect(() => {
    if (activeRun === null) return;
    // Keep polling through errors: the backend may be restarting.
    const timer = window.setInterval(() => void pollRun(activeRun).catch(() => undefined), RUN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [activeRun, pollRun]);

  const active = activeRun !== null ? runs[activeRun] : undefined;
  const toolCount = active?.tool_calls.length ?? 0;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, activeRun, toolCount]);

  async function send() {
    const content = draft.trim();
    if (!content || activeRun !== null || !ready) return;
    setError(null);
    try {
      const { run_id } = await api.sendMessage(agent.id, content);
      setDraft("");
      await load();
      setActiveRun(run_id);
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

  return (
    <section className="chat">
      <div className="messages">
        {messages.length === 0 && (
          <div className="chat-empty">
            <h2>What should {agent.name} do?</h2>
            <p>
              It has its own computer with a shell, files and a Chrome browser, and thinks with{" "}
              <strong>{agent.model}</strong> via {agent.provider}.
            </p>
          </div>
        )}
        {messages.map((message, index) => {
          const run = message.run_id !== null ? runs[message.run_id] : undefined;
          const isLastOfRun = messages[index + 1]?.run_id !== message.run_id;
          const finished = run && run.status !== "running";
          return (
            <div key={message.id}>
              {message.role === "assistant" && run && run.tool_calls.length > 0 && (
                <ToolCalls agentId={agent.id} calls={run.tool_calls} />
              )}
              {message.role === "assistant" ? (
                <div className="message assistant">
                  <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
                    {message.content}
                  </Markdown>
                </div>
              ) : (
                <div className="message user">{message.content}</div>
              )}
              {message.role === "user" && isLastOfRun && finished && (
                <>
                  {run.tool_calls.length > 0 && <ToolCalls agentId={agent.id} calls={run.tool_calls} />}
                  <div className="run-note">
                    {run.error ?? `The run ended without a reply (${run.status}).`}
                  </div>
                </>
              )}
            </div>
          );
        })}
        {activeRun !== null && (
          <>
            {active && active.tool_calls.length > 0 && (
              <ToolCalls agentId={agent.id} calls={active.tool_calls} />
            )}
            <div className="thinking">
              <span /> <span /> <span />
            </div>
          </>
        )}
        <div ref={bottom} />
      </div>

      {error && <div className="notice error">{error}</div>}
      <form className="composer" onSubmit={handleSubmit}>
        <textarea
          rows={1}
          placeholder={ready ? `Message ${agent.name}` : "Turn on this agent's computer to chat"}
          disabled={!ready}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKey}
        />
        {activeRun !== null ? (
          <button
            type="button"
            className="send stop"
            aria-label="Stop"
            title="Stop the agent"
            onClick={() => void api.cancelRun(agent.id).then(() => pollRun(activeRun))}
          >
            ■
          </button>
        ) : (
          <button type="submit" className="send" aria-label="Send" disabled={!ready || !draft.trim()}>
            ↑
          </button>
        )}
      </form>
      <div className="composer-foot">
        Requests go to {agent.provider} ({agent.model}). Actions run on the agent's own computer.
      </div>

      {active?.pending_approval && (
        <ApprovalModal
          key={active.pending_approval.id}
          agentName={agent.name}
          approval={active.pending_approval}
          onAnswered={() => void pollRun(active.id)}
        />
      )}
    </section>
  );
}
