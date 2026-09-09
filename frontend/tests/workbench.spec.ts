import { expect, test } from "@playwright/test";

test("renders the workbench intake screen", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "把 Issue 变成可审查的贡献。" })).toBeVisible();
  await expect(page.getByRole("button", { name: "开始分析" })).toBeVisible();
});

test("restores a task snapshot from the URL after refresh", async ({ page }) => {
  const task = {
    task_id: "task-e2e",
    thread_id: "thread-e2e",
    repo: "acme/project",
    issue_number: 7,
    status: "awaiting_plan_approval",
    base_sha: "a".repeat(40),
    current_artifact_id: null,
    error: null,
    created_at: "2026-09-08T00:00:00Z",
    updated_at: "2026-09-08T00:00:00Z",
  };
  await page.route("**/api/v1/tasks/task-e2e", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: task }) }),
  );
  await page.route("**/api/v1/tasks/task-e2e/approvals", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [] }) }),
  );
  await page.route("**/api/v1/tasks/task-e2e/artifacts", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [] }) }),
  );
  await page.route("**/api/v1/tasks/task-e2e/events**", async (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );

  await page.goto("/?task=task-e2e");
  await expect(page.getByText("acme/project #7")).toBeVisible();
  await expect(page.getByText("等待方案审批")).toBeVisible();
  await expect(page.getByRole("button", { name: "继续" })).toBeDisabled();
});

test("submits a Patch for its own approval gate", async ({ page }) => {
  let status = "drafting_patch";
  const planArtifact = {
    artifact_id: "plan-1",
    task_id: "task-patch",
    kind: "plan_json",
    version: 1,
    path: "plan.json",
    sha256: "a".repeat(64),
    base_sha: "b".repeat(40),
    created_at: "2026-09-08T00:00:00Z",
  };
  const patchArtifact = { ...planArtifact, artifact_id: "patch-1", kind: "patch" };
  const approval = {
    approval_id: "approval-patch",
    task_id: "task-patch",
    kind: "patch",
    artifact_hash: "c".repeat(64),
    base_sha: "b".repeat(40),
    decision: null,
    feedback: null,
    consumed_at: null,
    created_at: "2026-09-08T00:00:00Z",
  };
  const task = {
    task_id: "task-patch",
    thread_id: "thread-patch",
    repo: "acme/project",
    issue_number: 8,
    status,
    base_sha: "b".repeat(40),
    current_artifact_id: "plan-1",
    error: null,
    created_at: "2026-09-08T00:00:00Z",
    updated_at: "2026-09-08T00:00:00Z",
  };

  await page.route("**/api/v1/tasks/task-patch", async (route) => {
    if (route.request().method() === "POST") {
      status = "awaiting_patch_approval";
      task.status = status;
      task.current_artifact_id = "patch-1";
      await route.fulfill({ status: 200, body: JSON.stringify({ data: task }) });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: { ...task, status } }),
    });
  });
  await page.route("**/api/v1/tasks/task-patch/approvals", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: status === "awaiting_patch_approval" ? [approval] : [] }),
    }),
  );
  await page.route("**/api/v1/tasks/task-patch/artifacts", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: status === "awaiting_patch_approval" ? [planArtifact, patchArtifact] : [planArtifact] }),
    }),
  );
  await page.route("**/api/v1/tasks/task-patch/artifacts/*", async (route) =>
    route.fulfill({ status: 200, contentType: "text/plain", body: "# plan\n" }),
  );
  await page.route("**/api/v1/tasks/task-patch/events**", async (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );
  await page.route("**/api/v1/tasks/task-patch/patches", async (route) => {
    status = "awaiting_patch_approval";
    task.status = status;
    task.current_artifact_id = "patch-1";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: task }) });
  });

  await page.goto("/?task=task-patch");
  await expect(page.getByLabel("Patch diff")).toBeVisible();
  await page.getByLabel("Patch diff").fill(
    "--- a/src/slugger.py\n+++ b/src/slugger.py\n@@ -1 +1 @@\n-old\n+new\n",
  );
  await page.getByRole("button", { name: "保存 Patch 并请求审批" }).click();
  await expect(page.getByRole("button", { name: "批准 Patch" })).toBeVisible();
});

test("runs the review gate and shows subagent evidence", async ({ page }) => {
  let status = "reviewing";
  const task = {
    task_id: "task-review",
    thread_id: "thread-review",
    repo: "acme/project",
    issue_number: 9,
    status,
    base_sha: "a".repeat(40),
    current_artifact_id: "test-report",
    error: null,
    created_at: "2026-09-08T00:00:00Z",
    updated_at: "2026-09-08T00:00:00Z",
  };
  const report = {
    artifact_id: "test-report",
    task_id: task.task_id,
    kind: "test_report",
    version: 1,
    sha256: "a".repeat(64),
    base_sha: task.base_sha,
    created_at: task.created_at,
  };

  await page.route("**/api/v1/tasks/task-review", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { ...task, status } }) });
  });
  await page.route("**/api/v1/tasks/task-review/approvals", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [] }) });
  });
  await page.route("**/api/v1/tasks/task-review/artifacts", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [report] }) });
  });
  await page.route("**/api/v1/tasks/task-review/artifacts/*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/v1/tasks/task-review/events**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `id: 1\nevent: subagent.completed\ndata: ${JSON.stringify({ event_id: 1, task_id: task.task_id, event_type: "subagent.completed", payload: { agent: "repo-explorer", status: "completed", duration_seconds: 0.12, summary: "read 2 file(s)" }, created_at: task.created_at })}\n\n`,
    });
  });
  await page.route("**/api/v1/tasks/task-review/reviews", async (route) => {
    status = "ready_to_publish";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { ...task, status } }) });
  });

  await page.goto("/?task=task-review");
  await expect(page.getByRole("button", { name: "运行只读评审" })).toBeVisible();
  await expect(page.getByTestId("agent-runs")).toContainText("Repo Explorer");
  await page.getByRole("button", { name: "运行只读评审" }).click();
  await expect(page.getByText("准备发布")).toBeVisible();
});

test("creates a publish approval card and saves an explicit preference", async ({ page }) => {
  let status = "ready_to_publish";
  let memories = [] as Array<Record<string, string>>;
  const task = {
    task_id: "task-publish",
    thread_id: "thread-publish",
    repo: "acme/project",
    issue_number: 10,
    status,
    base_sha: "a".repeat(40),
    current_artifact_id: "review-report",
    error: null,
    created_at: "2026-09-08T00:00:00Z",
    updated_at: "2026-09-08T00:00:00Z",
  };
  const approval = {
    approval_id: "approval-publish",
    task_id: task.task_id,
    kind: "publish",
    artifact_hash: "c".repeat(64),
    base_sha: task.base_sha,
    decision: null,
    feedback: null,
    consumed_at: null,
    created_at: task.created_at,
  };
  const artifacts = [
    "plan-json",
    "patch",
    "test-report",
    "review-report",
  ].map((artifact_id, index) => ({
    artifact_id,
    task_id: task.task_id,
    kind: artifact_id,
    version: 1,
    sha256: `${String.fromCharCode(97 + index).repeat(64)}`,
    base_sha: task.base_sha,
    created_at: task.created_at,
  }));

  await page.route("**/api/v1/memories?scope=preferences", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: memories }) }),
  );
  await page.route("**/api/v1/memories", async (route) => {
    if (route.request().method() === "POST") {
      const payload = route.request().postDataJSON() as { key: string; value: string };
      memories = [{ memory_id: "memory-1", namespace: "preferences", ...payload }];
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ data: memories[0] }) });
      return;
    }
    if (route.request().method() === "PUT") {
      const payload = route.request().postDataJSON() as { value: string };
      memories = memories.map((memory) => ({ ...memory, value: payload.value }));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: memories[0] }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: memories }) });
  });
  await page.route("**/api/v1/memories/memory-1", async (route) => {
    const payload = route.request().postDataJSON() as { value: string };
    memories = memories.map((memory) => ({ ...memory, value: payload.value }));
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: memories[0] }) });
  });
  await page.route("**/api/v1/tasks/task-publish", async (route) => {
    if (route.request().method() === "POST") {
      status = "awaiting_publish_approval";
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { ...task, status } }) });
  });
  await page.route("**/api/v1/tasks/task-publish/approvals", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: status === "awaiting_publish_approval" ? [approval] : [] }) }),
  );
  await page.route("**/api/v1/tasks/task-publish/artifacts", async (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: artifacts }) }),
  );
  await page.route("**/api/v1/tasks/task-publish/artifacts/*", async (route) =>
    route.fulfill({ status: 200, contentType: "text/plain", body: "review evidence" }),
  );
  await page.route("**/api/v1/tasks/task-publish/events**", async (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  );
  await page.route("**/api/v1/tasks/task-publish/publish", async (route) => {
    status = "awaiting_publish_approval";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { ...task, status } }) });
  });

  await page.goto("/?task=task-publish");
  await expect(page.getByTestId("memory-panel")).toBeVisible();
  await expect(page.getByTestId("memory-panel")).toContainText("06 / MEMORY");
  await page.getByLabel("记忆键").fill("response_style");
  await page.getByLabel("记忆内容").fill("Use concise Markdown.");
  await page.getByRole("button", { name: "记住偏好" }).click();
  await expect(page.getByTestId("memory-panel")).toContainText("response_style");
  await page.getByRole("button", { name: "编辑记忆 response_style" }).click();
  await page.getByLabel("编辑记忆内容").fill("Use short Markdown.");
  await page.getByRole("button", { name: "保存记忆" }).click();
  await expect(page.getByTestId("memory-panel")).toContainText("Use short Markdown.");

  await page.getByLabel("Fork 用户名").fill("contributor");
  await page.getByLabel("发布标题").fill("Fix issue 10");
  await page.getByLabel("发布正文").fill("Verified by the isolated test runner.");
  await page.getByRole("button", { name: "创建发布审批卡" }).click();
  await expect(page.getByText("等待发布审批")).toBeVisible();
  await expect(page.getByRole("button", { name: "批准发布" })).toBeVisible();
});
