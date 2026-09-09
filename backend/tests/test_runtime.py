import json
import os
import subprocess
import sys
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deepcontrib.agent import (
    build_agent,
    invoke_with_thread,
    last_message_text,
    resolve_configured_model,
)
from deepcontrib.runtime import build_checkpoint_probe


class _ToolCallingFakeModel(GenericFakeChatModel):
    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_ToolCallingFakeModel":
        return self


def test_invoke_with_thread_passes_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeAgent:
        def invoke(
            self, payload: dict[str, Any], config: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append({"payload": payload, "config": config})
            return {"messages": [{"role": "assistant", "content": "ok"}]}

    result = invoke_with_thread(FakeAgent(), "hello", "thread-test")

    assert result["messages"][-1]["content"] == "ok"
    assert calls[0]["config"] == {"configurable": {"thread_id": "thread-test"}}


def test_invoke_with_thread_rejects_blank_values() -> None:
    class FakeAgent:
        def invoke(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    with pytest.raises(ValueError, match="prompt"):
        invoke_with_thread(FakeAgent(), " ", "thread")
    with pytest.raises(ValueError, match="thread_id"):
        invoke_with_thread(FakeAgent(), "hello", " ")


def test_configured_openai_model_can_use_chat_completions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-used-only-to-build-model")
    monkeypatch.setenv("DEEPCONTRIB_USE_RESPONSES_API", "false")

    model = resolve_configured_model("openai:qwen3.7-flash")

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "qwen3.7-flash"
    assert model.use_responses_api is False
    assert model.model_kwargs["parallel_tool_calls"] is False
    assert model.extra_body == {"enable_thinking": False}


def test_configured_model_keeps_deep_agents_default_without_transport_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPCONTRIB_USE_RESPONSES_API", raising=False)

    assert resolve_configured_model("openai:gpt-5.5") == "openai:gpt-5.5"


def test_configured_model_rejects_invalid_transport_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPCONTRIB_USE_RESPONSES_API", "sometimes")

    with pytest.raises(ValueError, match="must be set to true or false"):
        resolve_configured_model("openai:qwen3.7-flash")


def test_real_deep_agent_calls_probe_tool_with_deterministic_model() -> None:
    model = _ToolCallingFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "probe_tool",
                            "args": {"value": "hello"},
                            "id": "probe-call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="probe completed"),
            ]
        )
    )

    result = invoke_with_thread(
        build_agent(model=model),
        "Call probe_tool with hello.",
        "tool-thread",
    )

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.content for message in tool_messages] == ["probe:hello"]
    assert last_message_text(result) == "probe completed"


def test_interrupt_can_resume_with_same_thread() -> None:
    model = _ToolCallingFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "probe_tool",
                            "args": {"value": "approval"},
                            "id": "probe-call-2",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="approved"),
            ]
        )
    )
    agent = build_agent(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_on={"probe_tool": {"allowed_decisions": ["approve", "reject"]}},
    )
    config = {"configurable": {"thread_id": "approval-thread"}}

    paused = agent.invoke(
        {"messages": [{"role": "user", "content": "run the probe"}]},
        config,
        version="v2",
    )

    assert paused.interrupts
    action = paused.interrupts[0].value["action_requests"][0]
    assert action["name"] == "probe_tool"

    resumed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
        version="v2",
    )

    assert not resumed.interrupts
    assert last_message_text(resumed.value) == "approved"


def test_checkpoint_probe_pauses_and_resumes() -> None:
    graph = build_checkpoint_probe(InMemorySaver())
    config = {"configurable": {"thread_id": "checkpoint-probe-thread"}}

    paused = graph.invoke({"status": "started"}, config)
    resumed = graph.invoke(Command(resume={"approved": True}), config)

    assert paused["__interrupt__"][0].value["type"] == "checkpoint_probe"
    assert resumed == {"status": "resumed", "approval": "True"}


@pytest.mark.integration
def test_postgres_thread_can_resume_after_process_restart() -> None:
    """Run only when a caller supplies a disposable PostgreSQL instance."""
    from deepcontrib.checkpoint import CheckpointError, ensure_postgres_schema

    database_url = os.getenv("DEEPCONTRIB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set DEEPCONTRIB_TEST_DATABASE_URL to run the PostgreSQL check")

    try:
        ensure_postgres_schema(database_url)
    except CheckpointError as exc:
        pytest.skip(str(exc))

    process_code = """
import json
import os
import sys

from deepcontrib.checkpoint import postgres_checkpointer
from deepcontrib.runtime import build_checkpoint_probe
from langgraph.types import Command

database_url = os.environ["DEEPCONTRIB_TEST_DATABASE_URL"]
action = sys.argv[1]
with postgres_checkpointer(database_url) as checkpointer:
    graph = build_checkpoint_probe(checkpointer)
    config = {"configurable": {"thread_id": "postgres-restart-thread"}}
    if action == "pause":
        result = graph.invoke({"status": "started"}, config)
        print(json.dumps({"paused": "__interrupt__" in result}))
    else:
        result = graph.invoke(Command(resume={"approved": True}), config)
        print(json.dumps(result))
"""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    first = subprocess.run(
        [sys.executable, "-c", process_code, "pause"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    second = subprocess.run(
        [sys.executable, "-c", process_code, "resume"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(first.stdout) == {"paused": True}
    assert json.loads(second.stdout) == {"status": "resumed", "approval": "True"}


@pytest.mark.integration
def test_build_agent_uses_real_deep_agents_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("deepagents")
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-used-only-to-build-agent")

    agent = build_agent(model="openai:gpt-5.5")

    assert hasattr(agent, "invoke")
