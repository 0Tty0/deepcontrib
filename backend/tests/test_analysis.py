from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from deepcontrib.analysis import (
    AnalysisContext,
    AnalysisError,
    build_analysis_agent,
    build_analysis_tools,
    build_deterministic_plan,
    prepare_analysis,
    run_model_analysis,
)
from deepcontrib.github import IssueSnapshot
from deepcontrib.plan import ImplementationPlan
from deepcontrib.repository import RepositorySnapshot, parse_repository_url


class _ToolCallingFakeModel(GenericFakeChatModel):
    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_ToolCallingFakeModel":
        return self


def _context(root: Path | None = None) -> AnalysisContext:
    fixture_root = (
        root or Path(__file__).parents[2] / "tests" / "fixtures" / "python-bug"
    )
    return AnalysisContext(
        repo=parse_repository_url("https://github.com/example/python-bug"),
        issue=IssueSnapshot(
            number=1,
            title="Normalize repeated spaces in generated slugs",
            body="slugify emits multiple separators; update src/slugger.py.",
            state="open",
            html_url="https://github.com/example/python-bug/issues/1",
            comments=("Please add a test.",),
            comments_truncated=True,
        ),
        base_sha="a" * 40,
        snapshot=RepositorySnapshot(root=fixture_root, base_sha="a" * 40),
    )


def test_deterministic_fixture_plan_has_code_evidence() -> None:
    context = _context()

    plan = build_deterministic_plan(context)

    assert plan.repo == "example/python-bug"
    assert plan.base_sha == "a" * 40
    assert any(reference.path == "src/slugger.py" for reference in plan.relevant_files)
    assert any("regression" in item.lower() for item in plan.approach)


def test_analysis_tools_expose_only_read_operations() -> None:
    context = _context()
    tools = {tool.__name__: tool for tool in build_analysis_tools(context)}

    issue = tools["get_issue_context"]()
    files = tools["list_repository_files"]()
    excerpt = tools["read_repository_file"]("src/slugger.py")
    matches = tools["search_repository"]("slugify")

    assert '"base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' in issue
    assert any(item["path"] == "src/slugger.py" for item in files)
    assert '"base_sha"' in excerpt
    assert any(item["path"] == "src/slugger.py" for item in matches)


def test_prepare_analysis_uses_validated_inputs_and_fixed_sha(tmp_path: Path) -> None:
    class FakeClient:
        def get_issue(self, repo: Any, number: int) -> IssueSnapshot:
            assert repo.full_name == "example/python-bug"
            assert number == 3
            return _context().issue

        def get_default_branch_sha(self, repo: Any) -> str:
            assert repo.full_name == "example/python-bug"
            return "b" * 40

        def download_snapshot(
            self, repo: Any, base_sha: str, destination: Path
        ) -> RepositorySnapshot:
            assert repo.full_name == "example/python-bug"
            assert base_sha == "b" * 40
            destination.mkdir()
            (destination / "main.py").write_text("print('ok')\n", encoding="utf-8")
            return RepositorySnapshot(destination, base_sha)

    context = prepare_analysis(
        "https://github.com/example/python-bug",
        "3",
        client=FakeClient(),
        snapshot_root=tmp_path / "snapshot",
    )

    assert context.base_sha == "b" * 40
    assert context.snapshot.root.name == "snapshot"


def test_build_analysis_agent_has_real_deep_agent_graph() -> None:
    agent = build_analysis_agent(_ToolCallingFakeModel(messages=iter([])), _context())

    assert hasattr(agent, "invoke")


def test_run_model_analysis_requires_structured_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "deepcontrib.analysis.build_analysis_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.analysis.invoke_with_thread",
        lambda *args, **kwargs: {"messages": []},
    )

    with pytest.raises(AnalysisError, match="structured"):
        run_model_analysis(
            _context(),
            model="fake",
            thread_id="thread",
            artifact_directory=tmp_path,
        )


def test_run_model_analysis_validates_and_binds_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = ImplementationPlan(
        repo="wrong/repo",
        issue_number=99,
        issue_title="wrong",
        base_sha="f" * 40,
        problem="candidate",
    )
    monkeypatch.setattr(
        "deepcontrib.analysis.build_analysis_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.analysis.invoke_with_thread",
        lambda *args, **kwargs: {"structured_response": candidate},
    )

    plan, artifact = run_model_analysis(
        _context(),
        model="fake",
        thread_id="thread",
        artifact_directory=tmp_path,
    )

    assert plan.repo == "example/python-bug"
    assert plan.issue_number == 1
    assert plan.base_sha == "a" * 40
    assert artifact.json_path.is_file()


def test_run_model_analysis_rejects_invalid_model_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "deepcontrib.analysis.build_analysis_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.analysis.invoke_with_thread",
        lambda *args, **kwargs: {"structured_response": {"repo": "bad"}},
    )

    with pytest.raises(AnalysisError, match="invalid"):
        run_model_analysis(
            _context(),
            model="fake",
            thread_id="thread",
            artifact_directory=tmp_path,
        )
