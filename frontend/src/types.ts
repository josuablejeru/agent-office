export type VmStatus = "not_created" | "stopped" | "running";

export type Activity = "idle" | "working" | "needs_approval";

export type Level = "allow" | "ask" | "off";
export type UnknownAction = "default" | "allow" | "ask";

export interface PermissionsInfo {
  groups: { key: string; name: string; description: string }[];
  always_asks: string[];
  app_default: "allow" | "ask";
}

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
  permissions: Record<string, Level>;
  unknown_action: UnknownAction;
  vision_enabled: boolean;
  /** Tasks go to the model Auto chooses instead of this agent's own. */
  auto_model: boolean;
  mcp_servers: string[];
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
  permissions: Record<string, Level>;
  unknown_action: UnknownAction;
  vision_enabled: boolean;
  /** Tasks go to the model Auto chooses instead of this agent's own. */
  auto_model: boolean;
  mcp_servers: string[];
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

export interface PackageRepository {
  url: string;
  username: string;
  pypi_repo: string;
  npm_repo: string;
  base_image: string;
  has_token: boolean;
}

export interface PackageRepositoryUpdate {
  url: string;
  username: string;
  pypi_repo: string;
  npm_repo: string;
  base_image: string;
  token: string;
}

export interface DnsStatus {
  active: boolean;
  default_servers: string[];
  rules: Record<string, string[]>;
  search_domains: string[];
  certificates: { subject: string; expires: string; source: string; fingerprint: string }[];
}

export interface AppSettings {
  keep_vms_running_on_quit: boolean;
  check_updates: boolean;
  update_channel: "stable" | "beta";
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

export interface UpdateStatus {
  current: string;
  latest: string;
  available: boolean;
  notes: string;
  published: string;
  state: "idle" | "checking" | "downloading" | "ready" | "installing" | "error";
  progress: number;
  detail: string;
  can_install: boolean;
  install_note: string;
  checked_at: number;
  dismissed: string;
  channel: "stable" | "beta";
}

export interface BaseImageStatus {
  ready: boolean;
  building: boolean;
  detail: string;
  source: string;
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

export type McpTransport = "stdio" | "http";

export interface McpServer {
  name: string;
  transport: McpTransport;
  url: string;
  auth_header: string;
  command: string;
  args: string[];
  cwd: string;
  description: string;
  always_ask: boolean;
  has_token: boolean;
  env_keys: string[];
}

export interface McpServerForm {
  name: string;
  transport: McpTransport;
  url: string;
  auth_header: string;
  command: string;
  args: string[];
  cwd: string;
  description: string;
  always_ask: boolean;
  token: string;
  /** Empty value: keep the stored one. A name left out is removed. */
  env: Record<string, string>;
}

export interface ClaudeDesktopServer {
  name: string;
  transport: McpTransport;
  describe: string;
  env_keys: string[];
  has_token: boolean;
  exists: boolean;
}

export interface PendingApproval {
  id: number;
  tool: string;
  arguments: Record<string, unknown>;
  risk: string | null;
  reason: string | null;
}

export interface RoutingSettings {
  router: string;
  routes: { when: string; use: string }[];
  escalate_to: string;
  escalate_after: number;
}

export interface ModelChoice {
  label: string;
  provider: string;
  model: string;
}

export interface Run {
  id: number;
  agent_id: number;
  status: "running" | "completed" | "stopped" | "failed" | "interrupted" | "cancelled";
  error: string | null;
  /** The model that did the work; `switched` when it was not the agent's own. */
  provider: string;
  model: string;
  switched: boolean;
  /** The model was chosen by Auto; the note says how (and whether it escalated). */
  auto: boolean;
  route_note: string | null;
  tool_calls: ToolCall[];
  pending_approval: PendingApproval | null;
}
