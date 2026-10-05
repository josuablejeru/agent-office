export type VmStatus = "not_created" | "stopped" | "running";

export type Activity = "idle" | "working" | "needs_approval";

export interface Agent {
  id: number;
  name: string;
  system_prompt: string;
  avatar: string;
  activity: Activity;
  provider: string;
  model: string;
  fallback_provider: string | null;
  fallback_model: string | null;
  jev_enabled: boolean;
  perm_browser: boolean;
  perm_shell: boolean;
  perm_files: boolean;
  vm_memory_mb: number;
  vm_cpus: number;
  vm_status: VmStatus;
  created_at: string;
}

export interface AgentCreate {
  name: string;
  system_prompt: string;
  avatar: string;
  fallback_provider: string | null;
  fallback_model: string | null;
  provider: string;
  model: string;
  jev_enabled: boolean;
  perm_browser: boolean;
  perm_shell: boolean;
  perm_files: boolean;
  vm_memory_mb: number;
  vm_cpus: number;
}

export type AgentUpdate = Partial<Omit<AgentCreate, "name">>;

export interface Provider {
  name: string;
  type: string;
  base_url: string | null;
  key_name: string | null;
  has_key: boolean;
  uses_google_cloud: boolean;
  project: string | null;
  region: string | null;
  credentials_found: boolean;
}

export interface AppSettings {
  keep_vms_running_on_quit: boolean;
}

export interface ModelStatus {
  ok: boolean;
  detail: string;
}

export interface ModelCheck {
  ok: boolean;
  tools: boolean;
  seconds: number;
  detail: string;
}

export interface SharedFile {
  path: string;
  size: number;
  modified: number;
}

export interface Fact {
  id: number;
  subject: string;
  relation: string | null;
  object: string | null;
  note: string | null;
  updated: number;
}

export interface MemoryView {
  facts: Fact[];
  total_remembered: number;
  databases: { name: string; tables: { name: string; columns: string[]; rows: number }[] }[];
}

export interface Channel {
  id: number;
  name: string;
}

export interface ChannelMessage {
  id: number;
  author: string;
  content: string;
  created_at: string;
}

export interface BaseImageStatus {
  ready: boolean;
  building: boolean;
  detail: string;
}

export interface Capabilities {
  ready: boolean;
  qemu_version: string | null;
  problems: string[];
}

export interface Message {
  id: number;
  run_id: number | null;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ToolCall {
  id: number;
  tool: string;
  arguments: Record<string, unknown>;
  decision: string | null;
  result: Record<string, unknown> | null;
}

export interface PendingApproval {
  id: number;
  tool: string;
  arguments: Record<string, unknown>;
  risk: string | null;
  reason: string | null;
}

export interface Run {
  id: number;
  agent_id: number;
  status: "running" | "completed" | "stopped" | "failed" | "interrupted" | "cancelled";
  error: string | null;
  tool_calls: ToolCall[];
  pending_approval: PendingApproval | null;
}
