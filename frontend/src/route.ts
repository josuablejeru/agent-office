import { SETTINGS_SECTIONS, type SettingsSection } from "./SettingsPage";

/** What the main area shows; each has an address in the URL fragment, so reload and Back work. */
export type View =
  | { kind: "agent"; id: number }
  | { kind: "channel"; id: number }
  | { kind: "settings"; section: SettingsSection }
  | null;

const SECTIONS = new Set<string>(SETTINGS_SECTIONS.map(([key]) => key));

export function parseRoute(hash: string): View {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "settings") {
    const section = SECTIONS.has(parts[1] ?? "") ? (parts[1] as SettingsSection) : "general";
    return { kind: "settings", section };
  }
  const id = Number(parts[1]);
  if (parts[0] === "agents" && Number.isInteger(id)) return { kind: "agent", id };
  if (parts[0] === "channels" && Number.isInteger(id)) return { kind: "channel", id };
  return null;
}

export function routeFor(view: View): string {
  if (view === null) return "";
  if (view.kind === "settings") return `#/settings/${view.section}`;
  return `#/${view.kind === "agent" ? "agents" : "channels"}/${view.id}`;
}
