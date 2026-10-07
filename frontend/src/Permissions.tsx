import { useEffect, useState } from "react";
import { api } from "./api";
import type { Level, PermissionsInfo, UnknownAction } from "./types";

const LEVELS: [Level, string][] = [
  ["allow", "Allowed"],
  ["ask", "Ask me first"],
  ["off", "Not allowed"],
];

interface Props {
  levels: Record<string, Level>;
  unknownAction: UnknownAction;
  jevEnabled: boolean;
  onLevel: (group: string, level: Level) => void;
  onUnknownAction: (value: UnknownAction) => void;
  onJev: (enabled: boolean) => void;
}

/** What one agent may do: a level per kind of tool, on top of the built-in safety rules. */
export function Permissions({ levels, unknownAction, jevEnabled, onLevel, onUnknownAction, onJev }: Props) {
  const [info, setInfo] = useState<PermissionsInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .describePermissions()
      .then(setInfo)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  if (error) return <p className="form-error">{error}</p>;
  if (!info) return <p className="hint">Loading…</p>;

  const setAll = (level: Level) => info.groups.forEach((group) => onLevel(group.key, level));
  const appDefault = info.app_default === "ask" ? "ask me first" : "allowed";

  return (
    <div className="permissions">
      <div className="permission-presets">
        <span className="hint">Set everything to</span>
        <button type="button" onClick={() => setAll("allow")}>
          Allowed
        </button>
        <button type="button" onClick={() => setAll("ask")}>
          Ask me first
        </button>
      </div>

      {info.groups.map((group) => (
        <div className="permission-row" key={group.key}>
          <div className="permission-text">
            <strong>{group.name}</strong>
            <span>{group.description}</span>
          </div>
          <div className="segmented" role="radiogroup" aria-label={group.name}>
            {LEVELS.map(([level, label]) => (
              <button
                type="button"
                key={level}
                role="radio"
                aria-checked={(levels[group.key] ?? "allow") === level}
                className={(levels[group.key] ?? "allow") === level ? `active ${level}` : ""}
                onClick={() => onLevel(group.key, level)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      ))}

      <div className="permission-row">
        <div className="permission-text">
          <strong>Anything the safety rules do not recognise</strong>
          <span>
            Commands that are neither clearly harmless (like listing files) nor clearly risky. Only applies where the
            level above is "Allowed".
          </span>
        </div>
        <select value={unknownAction} onChange={(e) => onUnknownAction(e.target.value as UnknownAction)}>
          <option value="default">App default ({appDefault})</option>
          <option value="allow">Allowed</option>
          <option value="ask">Ask me first</option>
        </select>
      </div>

      <label className="toggle">
        <input type="checkbox" checked={jevEnabled} onChange={(e) => onJev(e.target.checked)} />
        Ask Jev about actions no built-in safety rule covers
      </label>

      <div className="always-asks">
        <strong>Always asks you, whatever is chosen above</strong>
        <ul>
          {info.always_asks.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
      <p className="hint">
        "Not allowed" takes the tool away from the agent. Changes apply to the agent's next action, even in the middle
        of a task. These settings govern the agent's tools; they do not limit what programs on its computer can reach
        on your network.
      </p>
    </div>
  );
}
