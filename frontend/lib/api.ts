export type TaskStatus =
  | "queued"
  | "analyzing"
  | "awaiting_plan_approval"
  | "drafting_patch"
  | "awaiting_patch_approval"
  | "testing"
  | "reviewing"
  | "ready_to_publish"
  | "awaiting_publish_approval"
  | "publishing"
  | "interrupted"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  task_id: string;
  thread_id: string;
  repo: string;
  issue_number: number;
  status: TaskStatus;
  base_sha: string | null;
  current_artifact_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Approval {
  approval_id: string;
  task_id: string;
  kind: "plan" | "patch" | "publish";
  artifact_hash: string;
  base_sha: string | null;
  decision: "approve" | "edit" | "reject" | null;
  feedback: string | null;
  consumed_at: string | null;
  created_at: string;
}

export interface Artifact {
  artifact_id: string;
  task_id: string;
  kind: string;
  version: number;
  sha256: string;
  base_sha: string | null;
  created_at: string;
}

export interface Memory {
  memory_id: string;
  namespace: string;
  key: string;
  value: string;
  source: string;
  repo: string | null;
  base_sha: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskEvent {
  event_id: number;
  task_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ApiErrorShape {
  error?: { message?: string; code?: string };
}

export const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

export function apiUrl(path: string): string {
  return `${apiBase}${path}`;
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorShape;
  if (!response.ok) {
    throw new Error(payload.error?.message ?? `Request failed (${response.status})`);
  }
  return payload as T;
}

export function openTaskEvents(
  taskId: string,
  afterId: number,
  onEvent: (event: TaskEvent) => void,
  onError: () => void,
): EventSource {
  const source = new EventSource(
    apiUrl(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/events?after_id=${afterId}`,
    ),
  );
  const eventNames = [
    "task.created",
    "task.status_changed",
    "task.stage_completed",
    "task.failed",
    "task.cancelled",
    "approval.required",
    "approval.approve",
    "approval.edit",
    "approval.reject",
    "patch.applied",
    "patch.apply_failed",
    "test.completed",
    "test.unsupported_environment",
    "test.failed",
    "subagent.started",
    "subagent.completed",
    "review.completed",
  ];
  const handle = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as TaskEvent);
    } catch {
      onError();
    }
  };
  eventNames.forEach((name) => source.addEventListener(name, handle));
  source.onerror = onError;
  return source;
}
