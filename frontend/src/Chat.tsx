import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import { CopyButton } from "./CopyButton";
import remarkGfm from "remark-gfm";
import { api } from "./api";
import { ApprovalModal } from "./ApprovalModal";
import { Screenshot } from "./Screenshot";
import { rememberModel, rememberedModel } from "./ModelList";
import { modelName, useModelSetup } from "./modelSetup";
import type { Agent, Message, ModelChoice, Run, ToolCall } from "./types";

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
              <div className="tool-output">
                <div className="message-actions">
                  {typeof call.result?.stdout === "string" && call.result.stdout && (
                    <CopyButton text={call.result.stdout as string} label="Copy output" />
                  )}
                  <CopyButton
                    text={JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2)}
                    label="Copy all"
                  />
                </div>
                <pre>{JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2)}</pre>
              </div>
            </details>
            {typeof screenshot === "string" && <Screenshot agentId={agentId} filename={screenshot} />}
          </div>
        );
      })}
    </div>
  );
}

const AUTO = "__auto__";
const OWN = "__own__";

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
  // The model for the next tasks: "" is the agent's default (its own model, or Auto when it is
  // set to Auto), AUTO or OWN the other of those two, otherwise a name from Settings.
  const { enabled, models, autoReady } = useModelSetup();
  const [chosen, setChosen] = useState(() => rememberedModel(agent.id));
  useEffect(() => setChosen(rememberedModel(agent.id)), [agent.id]);
  const agentAuto = agent.auto_model && autoReady;
  const ownName = modelName(models, agent.provider, agent.model);
  const isDefault = (model: ModelChoice) => model.provider === agent.provider && model.model === agent.model;
  // Anything that no longer applies (a removed name, Auto no longer set up) means the default.
  // Switched off in Settings: nothing chosen here applies.
  const explicitAuto = enabled && chosen === AUTO && autoReady && !agentAuto;
  const explicitOwn = enabled && chosen === OWN && agentAuto;
  const choice = (enabled && models.find((model) => model.label === chosen && !isDefault(model))) || null;
  const auto = explicitAuto || (agentAuto && !explicitOwn && !choice);
  const choose = (label: string) => {
    setChosen(label);
    rememberModel(agent.id, label);
  };

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
      const { run_id } = await api.sendMessage(
        agent.id,
        content,
        explicitAuto ? "auto" : explicitOwn ? "own" : choice,
      );
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
              {auto ? (
                <>
                  the model <strong>Auto</strong> picks for each task
                </>
              ) : (
                <>
                  <strong>{choice ? choice.label : ownName}</strong> via {choice ? choice.provider : agent.provider}
                </>
              )}
              .
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
                  <div className="message-actions">
                    {run && run.model && (
                      <span
                        className={`model-note${run.switched ? " switched" : ""}`}
                        title={[run.route_note, `${run.provider} · ${run.model}`].filter(Boolean).join("\n")}
                      >
                        {run.auto ? "Auto › " : ""}
                        {modelName(models, run.provider, run.model)}
                        {run.route_note?.includes("escalated") ? " ↑" : ""}
                      </span>
                    )}
                    <CopyButton text={message.content} />
                  </div>
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
        {enabled && models.length > 0 && (
          <select
            className="model-switch"
            aria-label="Model for this task"
            title="Model for the tasks you send"
            value={explicitAuto ? AUTO : explicitOwn ? OWN : choice ? choice.label : ""}
            disabled={activeRun !== null}
            onChange={(e) => choose(e.target.value)}
          >
            <option value="">{agentAuto ? "Auto" : ownName} (default)</option>
            {agentAuto ? <option value={OWN}>{ownName}</option> : autoReady && <option value={AUTO}>Auto</option>}
            {models
              .filter((model) => !isDefault(model))
              .map((model) => (
                <option key={model.label} value={model.label}>
                  {model.label}
                </option>
              ))}
          </select>
        )}
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
        {auto
          ? "Auto picks the model for each task (Settings › Models & routing)."
          : `Requests go to ${choice ? choice.provider : agent.provider} (${choice ? choice.model : agent.model}).`}{" "}
        Actions run on the agent&apos;s own computer.
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
