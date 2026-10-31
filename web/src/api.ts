// Platform-neutral API client and types (reusable by a future React Native client).
// Auth is an httpOnly session cookie set by the backend; no secrets live in the client.

export type TaskState =
  | "NEW" | "PLANNING" | "AWAITING_INPUT" | "AWAITING_APPROVAL" | "CALLING" | "CLARIFYING"
  | "VERIFYING" | "WAITING_RETRY" | "WAITING_FOLLOW_UP" | "COMPLETED" | "NEEDS_HUMAN_HELP"
  | "FAILED" | "CANCELLED";

export interface Me {
  id: string; email: string; display_name: string; language: string | null;
  approve_every_call: boolean; allow_auto_redial: boolean; timezone: string;
  quiet_start_hour: number | null; quiet_end_hour: number | null;
}
export interface Meta {
  languages: Record<string, string>; telephony_mode: string; vapid_public_key: string | null;
  integrations: { prisma_stt: boolean; timbre_tts: boolean; evon_llm: boolean };
  evon_mode: "remote" | "mock" | "anthropic"; llm_model: string | null;
}
export interface Contact { id: string; name: string; phone: string; language: string | null; opted_out: boolean }
export interface InfoItem { key: string; description: string; status: string; value: string | null; evidence: string | null }
export interface Turn { id: string; call_id: string; speaker: "agent" | "recipient"; text: string; language: string | null; intent: string | null; created_at: string }
export interface Approval { id: string; status: string; payload: Record<string, any>; reasons: string[]; created_at: string; decided_at: string | null }
export interface Call { id: string; mode: string; status: string; created_at: string; ended_at: string | null; failure_reason: string | null }
export interface TaskSummary {
  id: string; state: TaskState; goal: string | null; original_request: string; goal_for_requester: string | null;
  contact: { name: string | null; phone: string | null; status: string; id: string | null };
  requester_language: string; recipient_language: string | null; next_action: string | null;
  status_detail: string | null; result_summary: string | null; follow_up_at: string | null;
  follow_up_reason: string | null; follow_up_authorized: boolean; follow_up_due: boolean;
  manual_takeover: boolean; created_at: string; updated_at: string;
}
export interface Task extends TaskSummary {
  required_info: InfoItem[]; missing_inputs: string[]; risk_flags: string[]; pending_turn: boolean;
  active_call: { id: string; mode: string } | null; calls: Call[]; turns: Turn[]; approvals: Approval[];
  events: { actor: string; kind: string; detail: Record<string, any>; at: string }[];
}
export interface Notice { id: string; task_id: string | null; kind: string; title: string; body: string; read: boolean; created_at: string }

export class ApiError extends Error {
  constructor(public status: number, message: string, public retryable = false) { super(message); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method, credentials: "same-origin",
      headers: body instanceof FormData || body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "You're offline or the server can't be reached.", true);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = (data as any).detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ") : `Error ${res.status}`;
    throw new ApiError(res.status, msg, Boolean((data as any).retryable));
  }
  return data as T;
}

export interface EvonHealth { mode: string; reachable: boolean; model_available: boolean; detail: string | null; warnings: string[]; latency_ms?: number }

export const api = {
  meta: () => req<Meta>("GET", "/meta"),
  evonHealth: () => req<EvonHealth>("GET", "/evon/health"),
  me: () => req<Me>("GET", "/me"),
  register: (email: string, password: string, display_name: string) => req<Me>("POST", "/auth/register", { email, password, display_name }),
  login: (email: string, password: string) => req<Me>("POST", "/auth/login", { email, password }),
  logout: () => req("POST", "/auth/logout"),
  updateMe: (p: Partial<Me>) => req<Me>("PATCH", "/me", p),

  contacts: () => req<Contact[]>("GET", "/contacts"),
  addContact: (c: { name: string; phone: string; language?: string | null }) => req<Contact>("POST", "/contacts", c),
  deleteContact: (id: string) => req("DELETE", `/contacts/${id}`),

  tasks: () => req<TaskSummary[]>("GET", "/tasks"),
  task: (id: string) => req<Task>("GET", `/tasks/${id}`),
  createTask: (b: { request: string; contact_id?: string; contact_name?: string; contact_phone?: string; recipient_language?: string; requester_language?: string }) =>
    req<Task>("POST", "/tasks", b),
  deleteTask: (id: string) => req("DELETE", `/tasks/${id}`),
  inputs: (id: string, b: { note?: string; contact_id?: string; contact_name?: string; contact_phone?: string; recipient_language?: string }) =>
    req<Task>("POST", `/tasks/${id}/inputs`, b),
  decide: (id: string, aid: string, b: { decision: "approve" | "reject" | "edit"; instruction?: string; contact_phone?: string }) =>
    req<Task>("POST", `/tasks/${id}/approvals/${aid}`, b),
  retry: (id: string) => req<Task>("POST", `/tasks/${id}/retry`),
  followUp: (id: string, decision: "call" | "dismiss") => req<Task>("POST", `/tasks/${id}/follow-up`, { decision }),
  authorizeFollowUp: (id: string, authorized: boolean) => req<Task>("POST", `/tasks/${id}/follow-up-authorization`, { authorized }),
  cancel: (id: string) => req<Task>("POST", `/tasks/${id}/cancel`),
  takeover: (id: string) => req<Task>("POST", `/tasks/${id}/takeover`),
  resolve: (id: string) => req<Task>("POST", `/tasks/${id}/resolve`),
  simReply: (id: string, call_id: string, text: string, idempotency_key: string) =>
    req<Task>("POST", `/tasks/${id}/sim/reply`, { call_id, text, idempotency_key }),
  simOutcome: (id: string, call_id: string, outcome: string) => req<Task>("POST", `/tasks/${id}/sim/outcome`, { call_id, outcome }),

  stt: (audio: Blob, language: string) => {
    const f = new FormData();
    f.append("audio", audio, "speech.wav");
    f.append("language", language);
    return req<{ transcript: string; empty: boolean }>("POST", "/stt", f);
  },
  turnAudioUrl: (id: string, turnId: string) => `/api/tasks/${id}/turns/${turnId}/audio`,
  summaryAudioUrl: (id: string) => `/api/tasks/${id}/summary/audio`,

  demoContactReply: (id: string) =>
    req<{ text: string; english: string; audio_mp3_b64: string | null }>("POST", `/tasks/${id}/demo/contact-reply`),
  demoTranslate: (texts: string[]) => req<{ english: string[] }>("POST", "/demo/translate", { texts }),

  notifications: () => req<Notice[]>("GET", "/notifications"),
  markRead: () => req("POST", "/notifications/read"),
  subscribePush: (sub: PushSubscriptionJSON) => req("POST", "/push/subscribe", sub),
};

// Human-readable state labels — text, not color, carries meaning.
export const STATE_LABEL: Record<TaskState, string> = {
  NEW: "New", PLANNING: "Planning", AWAITING_INPUT: "Needs details", AWAITING_APPROVAL: "Needs approval",
  CALLING: "On call", CLARIFYING: "Clarifying", VERIFYING: "Verifying", WAITING_RETRY: "Call dropped",
  WAITING_FOLLOW_UP: "Follow-up set", COMPLETED: "Done", NEEDS_HUMAN_HELP: "Needs you",
  FAILED: "Failed", CANCELLED: "Cancelled",
};
export const LIVE_STATES: TaskState[] = ["PLANNING", "CALLING", "CLARIFYING", "VERIFYING"];
export const FACT_LABEL: Record<string, string> = {
  confirmed: "Confirmed", estimate: "Estimate", uncertain: "Unclear", unanswered: "Not answered",
  refused: "Declined", unknown: "Not yet asked",
};
