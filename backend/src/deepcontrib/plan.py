"""Structured implementation plans and durable artifact serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class PlanError(RuntimeError):
    """Raised when a plan artifact cannot be loaded or saved."""


class FileReference(BaseModel):
    """A cited file range from the fixed repository snapshot."""

    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    reason: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        parsed = PurePosixPath(normalized)
        if not normalized or parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError("file reference path must stay inside the snapshot")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> FileReference:
        if self.end_line < self.start_line:
            raise ValueError("file reference end_line must be >= start_line")
        return self


class ImplementationPlan(BaseModel):
    """The only plan shape accepted from an analysis model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    repo: str
    issue_number: int = Field(gt=0)
    issue_title: str
    base_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    problem: str
    non_goals: list[str] = Field(default_factory=list)
    relevant_files: list[FileReference] = Field(default_factory=list)
    approach: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("repo", "issue_title", "problem")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plan text fields must not be blank")
        return value.strip()

    def to_markdown(self) -> str:
        """Render a human-readable artifact without executing any content."""
        lines = [
            "# Implementation Plan",
            "",
            f"- Repository: `{self.repo}`",
            f"- Issue: `#{self.issue_number}` — {self.issue_title}",
            f"- Base SHA: `{self.base_sha}`",
            "",
            "## Problem",
            "",
            self.problem,
            "",
            "## Non-goals",
            "",
        ]
        lines.extend(f"- {item}" for item in self.non_goals or ["None recorded."])
        lines.extend(["", "## Relevant files", ""])
        if self.relevant_files:
            lines.extend(
                f"- `{item.path}:{item.start_line}-{item.end_line}` — {item.reason}"
                for item in self.relevant_files
            )
        else:
            lines.append("No files were cited.")
        lines.extend(["", "## Approach", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(self.approach, 1))
        lines.extend(["", "## Tests", ""])
        lines.extend(
            f"- `{item}`" for item in self.tests or ["No test command identified."]
        )
        lines.extend(["", "## Uncertainties", ""])
        lines.extend(f"- {item}" for item in self.uncertainties or ["None recorded."])
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class PlanArtifact:
    json_path: Path
    markdown_path: Path


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def save_plan(plan: ImplementationPlan, directory: Path) -> PlanArtifact:
    """Write JSON and Markdown copies under a caller-owned artifact directory."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "plan-v1.json"
    markdown_path = target / "plan-v1.md"
    try:
        _write_atomic(json_path, plan.model_dump_json(indent=2) + "\n")
        _write_atomic(markdown_path, plan.to_markdown())
    except OSError as exc:
        raise PlanError(f"could not save plan artifacts in {target}") from exc
    return PlanArtifact(json_path, markdown_path)


def load_plan(path: Path) -> ImplementationPlan:
    """Load and validate a JSON plan artifact."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return ImplementationPlan.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise PlanError(f"could not load a valid plan from {path}") from exc
