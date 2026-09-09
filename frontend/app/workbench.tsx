"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Approval,
  Artifact,
  Memory,
  Task,
  TaskStatus,
  TaskEvent,
  apiUrl,
  apiRequest,
  openTaskEvents,
} from "@/lib/api";

interface Envelope<T> {
  data: T;
}

const statusLabels: Record<string, string> = {
  queued: "排队中",
  analyzing: "分析中",
  awaiting_plan_approval: "等待方案审批",
  drafting_patch: "准备 Patch",
  awaiting_patch_approval: "等待 Patch 审批",
  testing: "测试中",
  reviewing: "审查中",
  ready_to_publish: "准备发布",
  awaiting_publish_approval: "等待发布审批",
  publishing: "发布中",
  interrupted: "等待恢复",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function statusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

function canResume(status: TaskStatus): boolean {
  return status === "queued" || status === "interrupted" || status === "drafting_patch";
}

function formatEvent(event: TaskEvent): string {
  return `${event.event_type} · ${new Date(event.created_at).toLocaleTimeString()}`;
}

function agentLabel(name: string): string {
  return name === "repo-explorer" ? "Repo Explorer" : name === "reviewer" ? "Reviewer" : name;
}

function publishUrl(content: string): string | null {
  try {
    const value = JSON.parse(content) as { pr_url?: unknown };
    return typeof value.pr_url === "string" && value.pr_url.startsWith("https://github.com/")
      ? value.pr_url
      : null;
  } catch {
    return null;
  }
}

export default function HomePage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const taskIdFromUrl = searchParams.get("task");
  const [repo, setRepo] = useState("https://github.com/example/project");
  const [issue, setIssue] = useState("1");
  const [task, setTask] = useState<Task | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [artifactContent, setArtifactContent] = useState("");
  const [patchDiff, setPatchDiff] = useState("");
  const [feedback, setFeedback] = useState("");
  const [forkOwner, setForkOwner] = useState("");
  const [publishTitle, setPublishTitle] = useState("");
  const [publishBody, setPublishBody] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryKey, setMemoryKey] = useState("");
  const [memoryValue, setMemoryValue] = useState("");
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingMemoryValue, setEditingMemoryValue] = useState("");
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const eventSource = useRef<EventSource | null>(null);

  const appendEvent = useCallback((event: TaskEvent) => {
    setEvents((current) =>
      current.some((item) => item.event_id === event.event_id)
        ? current
        : [...current, event].sort((a, b) => a.event_id - b.event_id),
    );
  }, []);

  const refresh = useCallback(async (taskId: string) => {
    const [taskResponse, approvalResponse, artifactResponse] = await Promise.all([
      apiRequest<Envelope<Task>>(`/api/v1/tasks/${taskId}`),
      apiRequest<Envelope<Approval[]>>(`/api/v1/tasks/${taskId}/approvals`),
      apiRequest<Envelope<Artifact[]>>(`/api/v1/tasks/${taskId}/artifacts`),
    ]);
    setTask(taskResponse.data);
    setApprovals(approvalResponse.data);
    setArtifacts(artifactResponse.data);
    if (taskResponse.data.current_artifact_id) {
      const artifact = await fetch(
        apiUrl(
          `/api/v1/tasks/${taskId}/artifacts/${taskResponse.data.current_artifact_id}`,
        ),
        { cache: "no-store" },
      );
      if (artifact.ok) setArtifactContent(await artifact.text());
    }
  }, []);

  const refreshMemories = useCallback(async () => {
    const response = await apiRequest<Envelope<Memory[]>>(
      "/api/v1/memories?scope=preferences",
    );
    setMemories(response.data);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshMemories().catch(() => {
        // The workbench remains useful when the API is not running yet.
      });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshMemories]);

  useEffect(() => {
    if (!taskIdFromUrl || task?.task_id === taskIdFromUrl) return;
    const timer = window.setTimeout(() => {
      void refresh(taskIdFromUrl).catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "读取任务失败"),
      );
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, task, taskIdFromUrl]);

  useEffect(() => {
    if (!task) return;
    eventSource.current?.close();
    const cursor = events.at(-1)?.event_id ?? 0;
    const source = openTaskEvents(
      task.task_id,
      cursor,
      (event) => {
        appendEvent(event);
        void refresh(task.task_id).catch((reason: unknown) =>
          setError(reason instanceof Error ? reason.message : "刷新任务失败"),
        );
      },
      () => eventSource.current?.close(),
    );
    eventSource.current = source;
    return () => source.close();
  }, [appendEvent, events, refresh, task]);

  const currentApproval = useMemo(
    () => approvals.findLast((item) => item.decision === null),
    [approvals],
  );

  const currentArtifact = useMemo(
    () => artifacts.find((item) => item.artifact_id === task?.current_artifact_id),
    [artifacts, task?.current_artifact_id],
  );

  const agentRuns = useMemo(() => {
    const runs = new Map<string, { status: string; duration?: number; summary?: string }>();
    events
      .filter((event) => event.event_type === "subagent.started" || event.event_type === "subagent.completed")
      .forEach((event) => {
        const agent = typeof event.payload.agent === "string" ? event.payload.agent : "unknown";
        runs.set(agent, {
          status: typeof event.payload.status === "string" ? event.payload.status : "unknown",
          duration: typeof event.payload.duration_seconds === "number" ? event.payload.duration_seconds : undefined,
          summary: typeof event.payload.summary === "string" ? event.payload.summary : undefined,
        });
      });
    return [...runs.entries()].map(([name, run]) => ({ name, ...run }));
  }, [events]);

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setArtifactContent("");
    setPatchDiff("");
    setArtifacts([]);
    setEvents([]);
    try {
      const response = await apiRequest<Envelope<Task>>("/api/v1/tasks", {
        method: "POST",
        body: JSON.stringify({ repo, issue: Number(issue) }),
      });
      router.replace(`/?task=${encodeURIComponent(response.data.task_id)}`);
      setTask(response.data);
      await refresh(response.data.task_id);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "创建任务失败");
    } finally {
      setBusy(false);
    }
  }

  async function mutate(path: string, init?: RequestInit) {
    if (!task) return;
    setBusy(true);
    setError("");
    try {
      await apiRequest<Envelope<Task>>(path, init);
      await refresh(task.task_id);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function decide(decision: "approve" | "edit" | "reject") {
    if (!task || !currentApproval) return;
    await mutate(`/api/v1/tasks/${task.task_id}/approvals/${currentApproval.approval_id}`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        artifact_hash: currentApproval.artifact_hash,
        base_sha: currentApproval.base_sha,
        feedback: feedback || null,
      }),
    });
    setFeedback("");
  }

  async function submitPatch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!task || !patchDiff.trim()) return;
    await mutate(`/api/v1/tasks/${task.task_id}/patches`, {
      method: "POST",
      body: JSON.stringify({ diff: patchDiff }),
    });
    setPatchDiff("");
  }

  async function saveMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!memoryKey.trim() || !memoryValue.trim()) return;
    setMemoryBusy(true);
    setError("");
    try {
      await apiRequest<Envelope<Memory>>("/api/v1/memories", {
        method: "POST",
        body: JSON.stringify({
          scope: "preferences",
          key: memoryKey.trim(),
          value: memoryValue.trim(),
          source: "user",
          remember: true,
        }),
      });
      await refreshMemories();
      setMemoryKey("");
      setMemoryValue("");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "保存记忆失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function deleteMemory(memoryId: string) {
    setMemoryBusy(true);
    setError("");
    try {
      await apiRequest<void>(`/api/v1/memories/${encodeURIComponent(memoryId)}`, {
        method: "DELETE",
      });
      await refreshMemories();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "删除记忆失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  function beginEditMemory(memory: Memory) {
    setEditingMemoryId(memory.memory_id);
    setEditingMemoryValue(memory.value);
  }

  async function saveEditedMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingMemoryId || !editingMemoryValue.trim()) return;
    setMemoryBusy(true);
    setError("");
    try {
      await apiRequest<Envelope<Memory>>(
        `/api/v1/memories/${encodeURIComponent(editingMemoryId)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            value: editingMemoryValue.trim(),
            source: "user",
            remember: true,
          }),
        },
      );
      await refreshMemories();
      setEditingMemoryId(null);
      setEditingMemoryValue("");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "编辑记忆失败");
    } finally {
      setMemoryBusy(false);
    }
  }

  async function preparePublish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!task || !forkOwner.trim() || !publishTitle.trim() || !publishBody.trim()) return;
    await mutate(`/api/v1/tasks/${task.task_id}/publish`, {
      method: "POST",
      body: JSON.stringify({
        fork_owner: forkOwner.trim(),
        title: publishTitle.trim(),
        body: publishBody.trim(),
        base_branch: "main",
      }),
    });
  }

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <p className="eyebrow">DEEPCONTRIB / LOCAL WORKBENCH</p>
          <h1>把 Issue 变成可审查的贡献。</h1>
          <p className="lede">
            先读取固定 SHA 的公开仓库，再由 Agent 生成带证据的方案。每一步都停在明确的审批边界。
          </p>
        </div>
        <div className="hero-mark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      </header>

      <section className="grid">
        <div className="panel intake">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">01 / INTAKE</p>
              <h2>创建分析任务</h2>
            </div>
            <span className="pill">只读起点</span>
          </div>
          <form onSubmit={createTask}>
            <label>
              GitHub 仓库
              <input value={repo} onChange={(event) => setRepo(event.target.value)} />
            </label>
            <label>
              Issue 编号
              <input
                type="number"
                min="1"
                value={issue}
                onChange={(event) => setIssue(event.target.value)}
              />
            </label>
            <button className="primary" disabled={busy} type="submit">
              {busy ? "处理中…" : "开始分析"}
            </button>
          </form>
          {error && <p className="error">{error}</p>}
          <p className="hint">服务只接受 HTTPS github.com/owner/repo，不会执行仓库代码。</p>
        </div>

        <div className="panel state-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">02 / STATE</p>
              <h2>任务状态</h2>
            </div>
            {task && <span className={`status status-${task.status}`}>{statusLabel(task.status)}</span>}
          </div>
          {task ? (
            <dl className="facts">
              <div><dt>Task</dt><dd>{task.task_id}</dd></div>
              <div><dt>Thread</dt><dd>{task.thread_id}</dd></div>
              <div><dt>Issue</dt><dd>{task.repo} #{task.issue_number}</dd></div>
              <div><dt>Base SHA</dt><dd>{task.base_sha ?? "等待快照"}</dd></div>
            </dl>
          ) : (
            <div className="empty">创建一个任务后，这里会显示固定版本和执行阶段。</div>
          )}
          <div className="actions">
            <button
              disabled={!task || busy || !canResume(task.status)}
              onClick={() => task && mutate(`/api/v1/tasks/${task.task_id}/resume`, { method: "POST" })}
            >
              继续
            </button>
            {task?.status === "testing" && (
              <button
                disabled={busy}
                onClick={() => void mutate(`/api/v1/tasks/${task.task_id}/tests`, { method: "POST" })}
              >
                运行隔离测试
              </button>
            )}
            {task?.status === "reviewing" && (
              <button
                disabled={busy}
                onClick={() => void mutate(`/api/v1/tasks/${task.task_id}/reviews`, { method: "POST" })}
              >
                运行只读评审
              </button>
            )}
            <button disabled={!task || busy} onClick={() => task && mutate(`/api/v1/tasks/${task.task_id}/cancel`, { method: "POST" })}>
              取消
            </button>
          </div>
          {agentRuns.length > 0 && (
            <div className="agent-runs" data-testid="agent-runs">
              <p className="eyebrow">SUBAGENTS</p>
              {agentRuns.map((run) => (
                <div className="agent-row" key={run.name}>
                  <strong>{agentLabel(run.name)}</strong>
                  <span>{run.status === "completed" ? "已完成" : "运行中"}</span>
                  {run.duration !== undefined && <span>{run.duration.toFixed(3)}s</span>}
                  {run.summary && <small>{run.summary}</small>}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="workspace-grid">
        <div className="panel plan-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">03 / ARTIFACT</p>
              <h2>
                {currentArtifact?.kind === "patch"
                  ? "Patch"
                  : currentArtifact?.kind === "review_report"
                    ? "Review report"
                    : currentArtifact?.kind === "publish_result"
                      ? "Publish result"
                    : "Implementation Plan"}
              </h2>
            </div>
            {task?.current_artifact_id && <span className="pill">SHA 已绑定</span>}
          </div>
          <pre className="artifact">
            {artifactContent || "方案产物会在分析完成后显示。"}
          </pre>
          {publishUrl(artifactContent) && (
            <a
              className="export-link"
              href={publishUrl(artifactContent) ?? undefined}
              rel="noreferrer"
              target="_blank"
            >
              打开 Draft PR
            </a>
          )}
          {task?.status === "drafting_patch" && (
            <>
              <p className="hint">
                方案已批准，系统正在自动生成 Patch。若自动生成失败，可在下方粘贴 unified diff 作为手动兜底。
              </p>
              <form className="patch-form" onSubmit={submitPatch}>
                <label>
                  Unified diff（手动兜底）
                  <textarea
                    aria-label="Patch diff"
                    placeholder="粘贴经过审查的 unified diff…"
                    value={patchDiff}
                    onChange={(event) => setPatchDiff(event.target.value)}
                  />
                </label>
                <button
                  className="primary"
                  disabled={busy || !patchDiff.trim()}
                  type="submit"
                >
                  保存 Patch 并请求审批
                </button>
              </form>
            </>
          )}
          {currentArtifact?.kind === "patch" && (
            <p className="hint">
              当前 Patch：v{currentArtifact.version}，已绑定 base SHA。
            </p>
          )}
        </div>

        <div className="panel approval-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">04 / HUMAN GATE</p><h2>审批</h2></div>
            {currentApproval && <span className="pill accent">需要操作</span>}
          </div>
          {currentApproval ? (
            <>
              <p className="approval-copy">
                {currentApproval.kind === "plan"
                  ? "批准对象绑定到当前 Plan 的摘要和 base SHA。编辑会重新分析，拒绝不会推进 Patch。"
                  : currentApproval.kind === "patch"
                    ? "批准对象绑定到当前 Patch 的摘要和 base SHA。批准后才会在任务副本中应用。"
                    : "批准后将按卡片内容检查工作树，向 Fork 推送唯一任务分支并创建 Draft PR。"}
              </p>
              <div className="hash"><span>Artifact SHA-256</span><code>{currentApproval.artifact_hash}</code></div>
              <div className="hash"><span>Base SHA</span><code>{currentApproval.base_sha}</code></div>
              <textarea
                aria-label="审批反馈"
                placeholder="可选：告诉 Agent 需要补充什么…"
                value={feedback}
                onChange={(event) => setFeedback(event.target.value)}
              />
              <div className="approval-actions">
                <button disabled={busy} onClick={() => void decide("reject")}>拒绝</button>
                <button disabled={busy} onClick={() => void decide("edit")}>要求修改</button>
                <button className="primary" disabled={busy} onClick={() => void decide("approve")}>
                  {currentApproval.kind === "plan"
                    ? "批准方案"
                    : currentApproval.kind === "patch"
                      ? "批准 Patch"
                      : "批准发布"}
                </button>
              </div>
            </>
          ) : (
            <div className="empty">任务进入待审批阶段后，审批卡会出现在这里。</div>
          )}
        </div>
      </section>

      {task?.status === "ready_to_publish" && (
        <section className="panel publish-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">PUBLISH CARD</p><h2>准备 Draft PR</h2></div>
            <span className="pill accent">会产生远程副作用</span>
          </div>
          <p className="hint publish-warning">
            这里只生成发布卡，不会写入 GitHub。系统会自动在正文追加 Fixes #{task.issue_number}，将 Draft PR 关联到这个 Issue；批准下一张审批卡后才会执行 Fork、commit、push 和 Draft PR。
          </p>
          <form onSubmit={preparePublish}>
            <label>
              Fork 用户名
              <input
                aria-label="Fork 用户名"
                placeholder="你的 GitHub 用户名"
                value={forkOwner}
                onChange={(event) => setForkOwner(event.target.value)}
              />
            </label>
            <label>
              发布标题
              <input
                aria-label="发布标题"
                placeholder={`Fix ${task.repo} #${task.issue_number}`}
                value={publishTitle}
                onChange={(event) => setPublishTitle(event.target.value)}
              />
            </label>
            <label>
              发布正文
              <textarea
                aria-label="发布正文"
                placeholder="说明修复内容和实际验证结果…"
                value={publishBody}
                onChange={(event) => setPublishBody(event.target.value)}
              />
            </label>
            <button
              className="primary"
              disabled={busy || !forkOwner.trim() || !publishTitle.trim() || !publishBody.trim()}
              type="submit"
            >
              创建发布审批卡
            </button>
          </form>
        </section>
      )}

      <section className="panel event-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">05 / TRACE</p><h2>事件记录</h2></div>
          <span className="pill">可从游标恢复</span>
        </div>
        <div className="event-list">
          {events.length ? events.map((event) => (
            <div className="event-row" key={event.event_id}>
              <span className="event-id">{String(event.event_id).padStart(3, "0")}</span>
              <span>{formatEvent(event)}</span>
              <code>{JSON.stringify(event.payload)}</code>
            </div>
          )) : <div className="empty">还没有事件。</div>}
        </div>
      </section>

      <section className="panel memory-panel" data-testid="memory-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">06 / MEMORY</p><h2>明确记住的偏好</h2></div>
          <span className="pill">仅用户确认</span>
        </div>
        <p className="hint">
          只保存你明确提交的偏好；仓库事实按仓库命名空间隔离，凭据和完整日志不会进入记忆。
        </p>
        <form onSubmit={saveMemory}>
          <label>
            记忆键
            <input
              aria-label="记忆键"
              placeholder="例如 response_style"
              value={memoryKey}
              onChange={(event) => setMemoryKey(event.target.value)}
            />
          </label>
          <label>
            记忆内容
            <textarea
              aria-label="记忆内容"
              placeholder="例如 Use concise Markdown."
              value={memoryValue}
              onChange={(event) => setMemoryValue(event.target.value)}
            />
          </label>
          <button className="primary" disabled={memoryBusy || !memoryKey.trim() || !memoryValue.trim()} type="submit">
            记住偏好
          </button>
        </form>
        <div className="memory-list">
          {memories.length ? memories.map((memory) => (
            <div className="memory-row" key={memory.memory_id}>
              <div className="memory-content">
                <strong>{memory.key}</strong>
                {editingMemoryId === memory.memory_id ? (
                  <form className="memory-edit-form" onSubmit={saveEditedMemory}>
                    <textarea
                      aria-label="编辑记忆内容"
                      value={editingMemoryValue}
                      onChange={(event) => setEditingMemoryValue(event.target.value)}
                    />
                    <div className="memory-edit-actions">
                      <button className="primary" disabled={memoryBusy || !editingMemoryValue.trim()} type="submit">
                        保存记忆
                      </button>
                      <button type="button" onClick={() => setEditingMemoryId(null)}>取消</button>
                    </div>
                  </form>
                ) : <p>{memory.value}</p>}
              </div>
              {editingMemoryId !== memory.memory_id && (
                <div className="memory-actions">
                  <button
                    aria-label={`编辑记忆 ${memory.key}`}
                    disabled={memoryBusy}
                    onClick={() => beginEditMemory(memory)}
                  >
                    编辑
                  </button>
                  <button
                    aria-label={`删除记忆 ${memory.key}`}
                    disabled={memoryBusy}
                    onClick={() => void deleteMemory(memory.memory_id)}
                  >
                    删除
                  </button>
                </div>
              )}
            </div>
          )) : <div className="empty">还没有保存的偏好。</div>}
        </div>
        {task && (
          <a className="export-link" href={apiUrl(`/api/v1/tasks/${task.task_id}/export`)}>
            导出当前任务包
          </a>
        )}
      </section>
    </main>
  );
}
