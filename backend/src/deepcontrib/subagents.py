"""Read-only sub-agent contracts used by the review stage.

The first implementation deliberately keeps these agents deterministic.  They
share the same bounded repository reader as the analysis stage and expose their
tool list so a future model-backed implementation can be checked against the
same no-write/no-execute contract.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Literal

from deepcontrib.repository import RepositoryError, RepositorySnapshot

ExplorerTool = Literal["list_files", "read_file", "grep"]
EXPLORER_TOOL_NAMES: tuple[ExplorerTool, ...] = (
    "list_files",
    "read_file",
    "grep",
)


@dataclass(frozen=True)
class ExplorerSymbol:
    """A bounded symbol and the direct calls visible in its syntax tree."""

    path: str
    name: str
    kind: Literal["function", "class"]
    start_line: int
    end_line: int
    calls: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "calls": list(self.calls),
        }


@dataclass(frozen=True)
class ExplorerResult:
    """The compact summary passed to the main agent and persisted as an artifact."""

    base_sha: str
    files: tuple[str, ...]
    symbols: tuple[ExplorerSymbol, ...]
    evidence: tuple[str, ...]
    uncertainties: tuple[str, ...]
    tool_names: tuple[str, ...] = EXPLORER_TOOL_NAMES
    can_write: bool = False
    can_execute: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_sha": self.base_sha,
            "files": list(self.files),
            "symbols": [item.as_dict() for item in self.symbols],
            "evidence": list(self.evidence),
            "uncertainties": list(self.uncertainties),
            "tool_names": list(self.tool_names),
            "can_write": self.can_write,
            "can_execute": self.can_execute,
        }


class RepoExplorer:
    """Inspect a fixed snapshot through list/read/search operations only."""

    tool_names = EXPLORER_TOOL_NAMES
    can_write = False
    can_execute = False

    def explore(
        self,
        snapshot: RepositorySnapshot,
        *,
        relevant_paths: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
    ) -> ExplorerResult:
        """Return symbols, direct calls, and evidence without touching the disk."""
        uncertainties: list[str] = []
        selected = sorted({item for item in (relevant_paths or ()) if item.strip()})
        if not selected:
            try:
                selected = [item.path for item in snapshot.list_files()[:100]]
            except RepositoryError as exc:
                return ExplorerResult(
                    base_sha=snapshot.base_sha,
                    files=(),
                    symbols=(),
                    evidence=(),
                    uncertainties=(str(exc),),
                )

        files: list[str] = []
        symbols: list[ExplorerSymbol] = []
        evidence: list[str] = []
        for path in selected:
            try:
                excerpt = snapshot.read_file(path, offset=1, limit=10_000)
            except RepositoryError as exc:
                uncertainties.append(f"{path}: {exc}")
                continue
            files.append(excerpt.path)
            content = excerpt.content
            if len(content.splitlines()) >= 10_000:
                uncertainties.append(f"{excerpt.path}: file excerpt was truncated")
            if path.casefold().endswith(".py"):
                try:
                    tree = ast.parse(content, filename=path)
                except SyntaxError as exc:
                    uncertainties.append(
                        f"{path}: Python syntax could not be parsed at line "
                        f"{exc.lineno or 0}"
                    )
                else:
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            kind: Literal["function", "class"] = "function"
                        elif isinstance(node, ast.ClassDef):
                            kind = "class"
                        else:
                            continue
                        calls = tuple(
                            sorted(
                                {
                                    name
                                    for child in ast.walk(node)
                                    if isinstance(child, ast.Call)
                                    for name in [_call_name(child.func)]
                                    if name
                                }
                            )
                        )
                        symbols.append(
                            ExplorerSymbol(
                                path=path,
                                name=node.name,
                                kind=kind,
                                start_line=node.lineno,
                                end_line=getattr(node, "end_lineno", node.lineno),
                                calls=calls,
                            )
                        )
                        evidence.append(
                            f"{path}:{node.lineno}-"
                            f"{getattr(node, 'end_lineno', node.lineno)}"
                        )

        if query and query.strip():
            try:
                matches = snapshot.grep(query, max_matches=20)
            except RepositoryError as exc:
                uncertainties.append(f"search: {exc}")
            else:
                evidence.extend(
                    f"{match.path}:{match.line_number}" for match in matches
                )

        return ExplorerResult(
            base_sha=snapshot.base_sha,
            files=tuple(sorted(set(files))),
            symbols=tuple(
                sorted(
                    symbols, key=lambda item: (item.path, item.start_line, item.name)
                )
            ),
            evidence=tuple(dict.fromkeys(evidence)),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
        )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None
