import { useEffect, useState } from "react";
import { api } from "./api";
import type { Agent, ModelChoice, RoutingSettings } from "./types";

/** The named models and Auto's routes, shared by every screen that shows a model. */
export interface ModelSetup {
  /** Model switching is on (Settings › Models & routing); off, every task uses its agent's own model. */
  enabled: boolean;
  models: ModelChoice[];
  routing: RoutingSettings | null;
  /** Auto can be used: a model that chooses, and at least one route. */
  autoReady: boolean;
}

let current: ModelSetup = { enabled: false, models: [], routing: null, autoReady: false };
let loading: Promise<void> | null = null;
const listeners = new Set<(setup: ModelSetup) => void>();

/** Load again (after Settings saved) and update every screen that uses it. */
export function refreshModelSetup(): Promise<void> {
  loading = Promise.all([
    api.modelSwitching().catch(() => ({ enabled: false })),
    api.modelChoices().catch(() => []),
    api.routing().catch(() => null),
  ]).then(
    ([switching, models, routing]) => {
      const enabled = switching.enabled;
      current = {
        enabled,
        models,
        routing,
        autoReady: enabled && !!routing && !!routing.router && routing.routes.length > 0,
      };
      listeners.forEach((listener) => listener(current));
    },
  );
  return loading;
}

export function useModelSetup(): ModelSetup {
  const [setup, setSetup] = useState(current);
  useEffect(() => {
    listeners.add(setSetup);
    if (!loading) void refreshModelSetup();
    else setSetup(current);
    return () => {
      listeners.delete(setSetup);
    };
  }, []);
  return setup;
}

/** The name a model goes by in the list, or its ID. */
export function modelName(models: ModelChoice[], provider: string, model: string): string {
  return models.find((choice) => choice.provider === provider && choice.model === model)?.label ?? model;
}

/** How an agent's model is shown: "Auto", or the model's name. */
export function agentModelLabel(agent: Agent, setup: ModelSetup): string {
  if (agent.auto_model && setup.autoReady) return "Auto";
  return modelName(setup.models, agent.provider, agent.model);
}
