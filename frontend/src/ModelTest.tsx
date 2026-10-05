import { useState } from "react";
import { api } from "./api";
import type { ModelCheck } from "./types";

/** A button that sends one small request to a model and says, in words, what happened. */
export function ModelTest({ provider, model }: { provider: string; model: string }) {
  const [result, setResult] = useState<ModelCheck | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setResult(null);
    try {
      setResult(await api.testModel(provider, model.trim()));
    } catch (err) {
      setResult({ ok: false, tools: false, seconds: 0, detail: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  const state = !result ? "" : !result.ok ? "bad" : result.tools ? "good" : "warn";
  return (
    <div className="model-test">
      <button type="button" disabled={busy || !provider || !model.trim()} onClick={() => void run()}>
        {busy ? "Testing…" : "Test"}
      </button>
      {result && (
        <span className={`model-test-result ${state}`}>
          {result.detail}
          {result.ok && ` (${result.seconds}s)`}
        </span>
      )}
    </div>
  );
}
