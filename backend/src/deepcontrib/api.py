"""FastAPI facade for the durable single-user task service."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from deepcontrib.config import load_settings
from deepcontrib.memory import (
    InMemoryMemoryStore,
    MemoryRecord,
    MemoryStore,
    MemoryStoreError,
    PostgresMemoryStore,
    memory_namespace,
)
from deepcontrib.models import (
    ApprovalDecision,
    ApprovalRecord,
    ArtifactRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from deepcontrib.patch_generation import build_patch_generator
from deepcontrib.publish import GhPublisher, Publisher
from deepcontrib.service import (
    ServiceError,
    TaskExecutor,
    TaskService,
    build_analysis_executor,
)
from deepcontrib.store import (
    PostgresTaskStore,
    StoreError,
    TaskStore,
)
from deepcontrib.test_runner import DockerTestRunner, TestRunner


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1, max_length=300)
    issue: int = Field(gt=0)
    thread_id: str | None = Field(default=None, min_length=1, max_length=80)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    artifact_hash: str = Field(min_length=64, max_length=64)
    base_sha: str | None = Field(default=None, min_length=40, max_length=40)
    feedback: str | None = Field(default=None, max_length=2_000)


class CreatePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff: str = Field(min_length=1, max_length=5 * 1024 * 1024)
    reproduction_diff: str | None = Field(
        default=None, min_length=1, max_length=5 * 1024 * 1024
    )


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["preferences", "repository"]
    repo: str | None = Field(default=None, min_length=1, max_length=300)
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=20_000)
    source: str = Field(default="user", min_length=1, max_length=120)
    base_sha: str | None = Field(
        default=None, min_length=40, max_length=40, pattern=r"^[0-9a-fA-F]{40}$"
    )
    remember: bool = False


class UpdateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=20_000)
    source: str = Field(default="user", min_length=1, max_length=120)
    base_sha: str | None = Field(
        default=None, min_length=40, max_length=40, pattern=r"^[0-9a-fA-F]{40}$"
    )
    remember: bool = False


class CreatePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fork_owner: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    base_branch: str = Field(default="main", min_length=1, max_length=100)


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    thread_id: str
    repo: str
    issue_number: int
    status: str
    base_sha: str | None
    current_artifact_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, task: TaskRecord) -> TaskResponse:
        return cls.model_validate(task.as_dict())


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    task_id: str
    kind: str
    artifact_hash: str
    base_sha: str | None
    decision: str | None
    feedback: str | None
    consumed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_record(cls, approval: ApprovalRecord) -> ApprovalResponse:
        return cls.model_validate(approval.as_dict())


class MemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    namespace: str
    key: str
    value: str
    source: str
    repo: str | None
    base_sha: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, memory: MemoryRecord) -> MemoryResponse:
        return cls.model_validate(memory.as_dict())


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    task_id: str
    kind: str
    version: int
    sha256: str
    base_sha: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, artifact: ArtifactRecord) -> ArtifactResponse:
        payload = artifact.as_dict()
        payload.pop("path", None)
        return cls.model_validate(payload)


def create_app(
    *,
    store: TaskStore | None = None,
    executor: TaskExecutor | None = None,
    auto_start: bool = True,
    artifact_root: Path | None = None,
    data_root: Path | None = None,
    test_runner: TestRunner | None = None,
    memory_store: MemoryStore | None = None,
    publisher: Publisher | None = None,
    patch_generator: Any | None = None,
) -> FastAPI:
    """Construct the API; tests can inject an in-memory store and executor."""
    settings = load_settings()
    selected_store: TaskStore = (
        store
        if store is not None
        else cast(TaskStore, PostgresTaskStore(settings.database_url))
    )
    selected_memory_store: MemoryStore = memory_store or (
        PostgresMemoryStore(settings.database_url)
        if store is None
        else InMemoryMemoryStore()
    )
    selected_executor = executor or build_analysis_executor(
        settings, memory_store=selected_memory_store
    )
    selected_artifact_root = artifact_root
    if selected_artifact_root is None and store is None:
        selected_artifact_root = settings.data_dir
    selected_data_root = data_root
    if selected_data_root is None and store is None:
        selected_data_root = settings.data_dir
    selected_test_runner = test_runner
    if selected_test_runner is None and settings.test_image:
        selected_test_runner = cast(TestRunner, DockerTestRunner(settings.test_image))
    selected_publisher = publisher or (GhPublisher() if store is None else None)
    selected_patch_generator = patch_generator or (
        build_patch_generator(settings) if store is None else None
    )
    service = TaskService(
        selected_store,
        executor=selected_executor,
        artifact_root=selected_artifact_root,
        data_root=selected_data_root,
        test_runner=selected_test_runner,
        memory_store=selected_memory_store,
        publisher=selected_publisher,
        patch_generator=selected_patch_generator,
        max_task_seconds=settings.max_task_seconds,
        max_model_calls=settings.max_model_calls,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        try:
            selected_store.initialize()
            selected_memory_store.initialize()
            cleanup_orphaned = getattr(selected_test_runner, "cleanup_orphaned", None)
            if callable(cleanup_orphaned):
                cleanup_orphaned()
            service.recover_interrupted_tasks()
        except StoreError:
            # Keep the detail out of the HTTP response; requests will return a
            # stable 503 while the process log retains the actionable cause.
            raise
        except MemoryStoreError:
            raise
        yield

    app = FastAPI(title="DeepContrib", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.middleware("http")
    async def protect_local_mutations(request: Request, call_next: Any) -> Response:
        if request.method in {"POST", "PUT", "DELETE"} and request.url.path.startswith(
            "/api/"
        ):
            origin = request.headers.get("origin")
            allowed = {"http://127.0.0.1:3000", "http://localhost:3000"}
            if origin and origin not in allowed:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "origin_forbidden",
                            "message": "request origin is not allowed",
                        }
                    },
                )
        return cast(Response, await call_next(request))

    app.state.service = service

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "data": {
                "status": "ok",
                "single_user": True,
                "storage": type(selected_store).__name__,
                "memory_storage": type(selected_memory_store).__name__,
                "test_sandbox": service.test_runner is not None,
                "publisher": service.publisher is not None,
                "limits": {
                    "max_task_seconds": service.max_task_seconds,
                    "max_model_calls": service.max_model_calls,
                },
            }
        }

    @app.exception_handler(ServiceError)
    async def handle_service_error(
        _request: Request, exc: ServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(StoreError)
    async def handle_store_error(_request: Request, _exc: StoreError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "storage_unavailable",
                    "message": "task storage is unavailable",
                }
            },
        )

    @app.exception_handler(MemoryStoreError)
    async def handle_memory_error(
        _request: Request, exc: MemoryStoreError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_memory", "message": str(exc)}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(item) for item in error["loc"]),
                "message": error["msg"],
                "code": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "details": details,
                }
            },
        )

    @app.post("/api/v1/tasks", status_code=201)
    def create_task(
        payload: CreateTaskRequest, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        task = service.create_task(
            payload.repo,
            payload.issue,
            thread_id=payload.thread_id,
        )
        if auto_start:
            background_tasks.add_task(service.start_task, task.task_id)
        return JSONResponse(
            status_code=201,
            headers={"Location": f"/api/v1/tasks/{task.task_id}"},
            content={"data": TaskResponse.from_record(task).model_dump(mode="json")},
        )

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        response = TaskResponse.from_record(service.get_task(task_id))
        return {"data": response.model_dump(mode="json")}

    @app.get("/api/v1/tasks/{task_id}/events")
    def get_events(
        task_id: str,
        after_id: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        effective_after = after_id
        if last_event_id and last_event_id.isdigit():
            effective_after = max(effective_after, int(last_event_id))
        events = service.list_events(task_id, after_id=effective_after)

        def stream() -> Any:
            for event in events:
                payload = json.dumps(
                    event.as_dict(),
                    ensure_ascii=False,
                    default=lambda value: (
                        value.isoformat() if isinstance(value, datetime) else str(value)
                    ),
                )
                yield (
                    f"id: {event.event_id}\nevent: {event.event_type}"
                    f"\ndata: {payload}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/tasks/{task_id}/approvals")
    def get_approvals(task_id: str) -> dict[str, Any]:
        approvals = service.list_approvals(task_id)
        return {
            "data": [
                ApprovalResponse.from_record(item).model_dump(mode="json")
                for item in approvals
            ]
        }

    @app.get("/api/v1/tasks/{task_id}/artifacts/{artifact_id}")
    def get_artifact(task_id: str, artifact_id: str) -> Response:
        artifact, content = service.read_artifact(task_id, artifact_id)
        media_type = (
            "application/json"
            if artifact.kind.endswith("json")
            else "text/x-diff"
            if artifact.kind == "patch"
            else "text/markdown"
        )
        headers = {"X-Artifact-SHA256": artifact.sha256}
        if artifact.kind == "patch":
            headers["Content-Disposition"] = (
                f'attachment; filename="{artifact.artifact_id}.diff"'
            )
        return Response(
            content=content,
            media_type=media_type,
            headers=headers,
        )

    @app.get("/api/v1/tasks/{task_id}/artifacts")
    def get_artifacts(task_id: str) -> dict[str, Any]:
        return {
            "data": [
                ArtifactResponse.from_record(item).model_dump(mode="json")
                for item in service.list_artifacts(task_id)
            ]
        }

    @app.get("/api/v1/tasks/{task_id}/export")
    def export_task(task_id: str) -> Response:
        payload = service.export_task(task_id)
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="deepcontrib-{task_id}.zip"'
                )
            },
        )

    @app.post("/api/v1/tasks/{task_id}/patches")
    def create_patch(task_id: str, payload: CreatePatchRequest) -> dict[str, Any]:
        task, summary = service.create_patch(
            task_id, payload.diff, reproduction_diff=payload.reproduction_diff
        )
        return {
            "data": TaskResponse.from_record(task).model_dump(mode="json"),
            "summary": {
                "changed_paths": list(summary.changed_paths),
                "additions": summary.additions,
                "deletions": summary.deletions,
            },
        }

    @app.post("/api/v1/tasks/{task_id}/approvals/{approval_id}")
    def submit_approval(
        task_id: str,
        approval_id: str,
        payload: ApprovalRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        task = service.submit_approval(
            task_id,
            approval_id,
            payload.decision,
            artifact_hash=payload.artifact_hash,
            base_sha=payload.base_sha,
            feedback=payload.feedback,
        )
        if (
            task.status == TaskStatus.DRAFTING_PATCH
            and service.patch_generator is not None
        ):
            background_tasks.add_task(service.generate_patch, task.task_id)
        return {"data": TaskResponse.from_record(task).model_dump(mode="json")}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        task = service.cancel_task(task_id)
        return {"data": TaskResponse.from_record(task).model_dump(mode="json")}

    @app.post("/api/v1/tasks/{task_id}/tests")
    def run_tests(task_id: str) -> dict[str, Any]:
        task = service.run_tests(task_id)
        return {"data": TaskResponse.from_record(task).model_dump(mode="json")}

    @app.post("/api/v1/tasks/{task_id}/reviews")
    def run_review(task_id: str) -> dict[str, Any]:
        task = service.run_review(task_id)
        return {"data": TaskResponse.from_record(task).model_dump(mode="json")}

    @app.post("/api/v1/tasks/{task_id}/publish")
    def prepare_publish(task_id: str, payload: CreatePublishRequest) -> dict[str, Any]:
        task, request = service.prepare_publish(
            task_id,
            fork_owner=payload.fork_owner,
            title=payload.title,
            body=payload.body,
            base_branch=payload.base_branch,
        )
        return {
            "data": TaskResponse.from_record(task).model_dump(mode="json"),
            "publish": request.as_dict(),
        }

    @app.post("/api/v1/tasks/{task_id}/resume")
    def resume_task(task_id: str) -> dict[str, Any]:
        task = service.resume_task(task_id)
        return {"data": TaskResponse.from_record(task).model_dump(mode="json")}

    @app.get("/api/v1/memories")
    def list_memories(
        scope: Literal["preferences", "repository"],
        repo: str | None = Query(default=None, max_length=300),
    ) -> dict[str, Any]:
        namespace = memory_namespace(scope=scope, repo=repo)
        return {
            "data": [
                MemoryResponse.from_record(item).model_dump(mode="json")
                for item in selected_memory_store.list(namespace)
            ]
        }

    @app.post("/api/v1/memories", status_code=201)
    def create_memory(payload: CreateMemoryRequest) -> JSONResponse:
        if not payload.remember:
            raise ServiceError(
                "set remember=true to save a memory",
                code="explicit_memory_consent_required",
                status_code=422,
            )
        namespace = memory_namespace(scope=payload.scope, repo=payload.repo)
        repo_name = (
            namespace.removeprefix("repo:") if namespace.startswith("repo:") else None
        )
        now = utc_now()
        memory = selected_memory_store.upsert(
            MemoryRecord(
                memory_id=uuid4().hex,
                namespace=namespace,
                key=payload.key.strip(),
                value=payload.value,
                source=payload.source.strip(),
                repo=repo_name,
                base_sha=payload.base_sha.lower() if payload.base_sha else None,
                created_at=now,
                updated_at=now,
            )
        )
        return JSONResponse(
            status_code=201,
            content={
                "data": MemoryResponse.from_record(memory).model_dump(mode="json")
            },
        )

    @app.put("/api/v1/memories/{memory_id}")
    def update_memory(memory_id: str, payload: UpdateMemoryRequest) -> dict[str, Any]:
        if not payload.remember:
            raise ServiceError(
                "set remember=true to edit a memory",
                code="explicit_memory_consent_required",
                status_code=422,
            )
        existing = selected_memory_store.get(memory_id)
        if existing is None:
            raise ServiceError(
                "memory was not found", code="not_found", status_code=404
            )
        updated = selected_memory_store.upsert(
            replace(
                existing,
                value=payload.value,
                source=payload.source.strip(),
                base_sha=payload.base_sha.lower()
                if payload.base_sha
                else existing.base_sha,
                updated_at=utc_now(),
            )
        )
        return {"data": MemoryResponse.from_record(updated).model_dump(mode="json")}

    @app.delete("/api/v1/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str) -> Response:
        if not selected_memory_store.delete(memory_id):
            raise ServiceError(
                "memory was not found", code="not_found", status_code=404
            )
        return Response(status_code=204)

    return app


app = create_app()
