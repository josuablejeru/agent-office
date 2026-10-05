import type {
  Agent,
  AppSettings,
  BaseImageStatus,
  Channel,
  ChannelMessage,
  DnsStatus,
  MemoryView,
  ModelCheck,
  ModelStatus,
  SharedFile,
  AgentCreate,
  AgentUpdate,
  Capabilities,
  Message,
  Provider,
  Run,
} from "./types";

const TOKEN_KEY = "agent-office.api-token";

export class UnauthorizedError extends Error {}

/** The API token arrives once in the URL fragment and is then kept in this browser. */
export function loadToken(): string {
  const fromUrl = new URLSearchParams(window.location.hash.slice(1)).get("token");
  if (fromUrl) {
    saveToken(fromUrl);
    window.history.replaceState(null, "", window.location.pathname);
    return fromUrl;
  }
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(token: string) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage may be unavailable; the token then only lasts for this page load.
  }
}

let token = "";
export function setToken(value: string) {
  token = value;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...init?.headers,
    },
  });
  if (response.status === 401) throw new UnauthorizedError("Invalid API token");
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((item: { msg: string }) => item.msg).join("; ")
      : (detail ?? response.statusText);
    throw new Error(message);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

const post = (body?: unknown): RequestInit => ({
  method: "POST",
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const api = {
  listAgents: () => request<Agent[]>("/api/agents"),
  createAgent: (payload: AgentCreate) => request<Agent>("/api/agents", post(payload)),
  updateAgent: (id: number, payload: AgentUpdate) =>
    request<Agent>(`/api/agents/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteAgent: (id: number) =>
    request<void>(`/api/agents/${id}?confirm=true`, { method: "DELETE" }),
  startVm: (id: number) => request<Agent>(`/api/agents/${id}/vm/start`, post()),
  stopVm: (id: number) => request<Agent>(`/api/agents/${id}/vm/stop`, post()),
  guestStatus: (id: number) =>
    request<{ ready: boolean; problem: string | null }>(`/api/agents/${id}/vm/guest`),
  modelStatus: (id: number) => request<ModelStatus>(`/api/agents/${id}/model-status`),
  clearConversation: (id: number) =>
    request<void>(`/api/agents/${id}/messages`, { method: "DELETE" }),
  uploadAvatar: async (id: number, file: File) => {
    const response = await fetch(`/api/agents/${id}/avatar`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${token}` },
      body: file,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail ?? "The photo could not be uploaded.");
    }
  },
  avatarBlob: async (id: number) => {
    const response = await fetch(`/api/agents/${id}/avatar`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error("No photo");
    return response.blob();
  },
  testModel: (provider: string, model: string) =>
    request<ModelCheck>(`/api/providers/${provider}/test`, post({ model })),
  listFiles: (id: number) => request<SharedFile[]>(`/api/agents/${id}/files`),
  uploadFile: async (id: number, file: File) => {
    const response = await fetch(`/api/agents/${id}/files/${encodeURIComponent(file.name)}`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${token}` },
      body: file,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail ?? "The file could not be sent.");
    }
  },
  saveFile: (id: number, path: string) =>
    request<{ saved_to: string }>(`/api/agents/${id}/saved-files`, post({ path })),
  getMemory: (id: number, query: string) =>
    request<MemoryView>(`/api/agents/${id}/memory?query=${encodeURIComponent(query)}`),
  addFact: (id: number, fact: { subject: string; note: string; relation: string; object: string }) =>
    request<{ id: number }>(`/api/agents/${id}/memory`, post(fact)),
  forgetFact: (id: number, factId: number) =>
    request<void>(`/api/agents/${id}/memory/${factId}`, { method: "DELETE" }),
  listChannels: () => request<Channel[]>("/api/channels"),
  createChannel: (name: string) => request<Channel>("/api/channels", post({ name })),
  channelMessages: (id: number, after = 0) =>
    request<ChannelMessage[]>(`/api/channels/${id}/messages?after=${after}`),
  postToChannel: (id: number, content: string) =>
    request<ChannelMessage>(`/api/channels/${id}/messages`, post({ content })),
  dnsStatus: () => request<DnsStatus>("/api/system/dns"),
  getSettings: () => request<AppSettings>("/api/settings"),
  setSettings: (settings: AppSettings) =>
    request<AppSettings>("/api/settings", { method: "PUT", body: JSON.stringify(settings) }),
  setProviderLocation: (provider: string, project: string, region: string) =>
    request<void>(`/api/providers/${provider}/location`, {
      method: "PUT",
      body: JSON.stringify({ project, region }),
    }),
  listMessages: (id: number) => request<Message[]>(`/api/agents/${id}/messages`),
  sendMessage: (id: number, content: string) =>
    request<{ run_id: number }>(`/api/agents/${id}/chat`, post({ content })),
  getRun: (runId: number) => request<Run>(`/api/runs/${runId}`),
  cancelRun: (id: number) => request<{ cancelled: boolean }>(`/api/agents/${id}/chat/cancel`, post()),
  answerApproval: (approvalId: number, approved: boolean) =>
    request<{ approved: boolean }>(`/api/approvals/${approvalId}`, post({ approved })),
  /** Screenshots need the auth header, so they are fetched rather than linked. */
  screenshotBlob: async (id: number, filename: string) => {
    const response = await fetch(`/api/agents/${id}/screenshots/${filename}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error("Screenshot unavailable");
    return response.blob();
  },
  setManualControl: (id: number, manual: boolean) =>
    request<{ manual: boolean }>(`/api/agents/${id}/control`, {
      method: "PUT",
      body: JSON.stringify({ manual }),
    }),
  listProviders: () => request<Provider[]>("/api/providers"),
  addProvider: (name: string, base_url: string, needs_key: boolean) =>
    request<{ name: string }>("/api/providers", post({ name, base_url, needs_key })),
  setProviderKey: (provider: string, key: string) =>
    request<void>(`/api/providers/${provider}/key`, { method: "PUT", body: JSON.stringify({ key }) }),
  baseImage: () => request<BaseImageStatus>("/api/system/base-image"),
  buildBaseImage: () => request<BaseImageStatus>("/api/system/base-image", post()),
  listModels: (provider: string) => request<string[]>(`/api/providers/${provider}/models`),
  capabilities: () => request<Capabilities>("/api/system/capabilities"),
  vncUrl: (id: number) => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/api/agents/${id}/vnc`;
  },
  // WebSocket requests cannot carry headers, so the token travels as a subprotocol.
  vncProtocols: () => ["binary", `token.${token}`],
};
