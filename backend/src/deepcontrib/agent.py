"""Small Deep Agents harness used by the first milestone."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_openai import ChatOpenAI


def probe_tool(value: str) -> str:
    """Return a deterministic value so the first smoke run proves tool calling."""
    return f"probe:{value}"


SYSTEM_PROMPT = """You are the DeepContrib runtime smoke agent.

For a smoke request, call probe_tool exactly once with the requested value,
then report the tool result in one short sentence. Use write_todos only when
the user explicitly asks for a multi-step plan. Never claim a tool ran when it
did not run.
"""


def resolve_configured_model(model: Any) -> Any:
    """Resolve an OpenAI-compatible model with the configured API transport.

    Deep Agents defaults to the OpenAI Responses API for ``openai:...`` model
    strings.  Providers such as Qwen expose their compatible tool API through
    Chat Completions, so the transport can be selected explicitly without
    changing the provider/model setting.
    """
    if not isinstance(model, str):
        return model

    raw_transport = os.getenv("DEEPCONTRIB_USE_RESPONSES_API", "").strip().lower()
    if not raw_transport:
        return model
    if raw_transport in {"1", "true", "yes", "on"}:
        use_responses_api = True
    elif raw_transport in {"0", "false", "no", "off"}:
        use_responses_api = False
    else:
        raise ValueError("DEEPCONTRIB_USE_RESPONSES_API must be set to true or false")

    provider, separator, model_name = model.partition(":")
    if provider != "openai" or not separator or not model_name:
        return model
    model_kwargs: dict[str, Any] = {"parallel_tool_calls": False}
    extra_body: dict[str, Any] | None = None
    if model_name.casefold().startswith("qwen"):
        extra_body = {"enable_thinking": False}
    return ChatOpenAI(
        model=model_name,
        model_kwargs=model_kwargs,
        extra_body=extra_body,
        use_responses_api=use_responses_api,
    )


def build_agent(
    model: Any | None = None,
    *,
    checkpointer: Any | None = None,
    interrupt_on: dict[str, Any] | None = None,
) -> Any:
    """Build the smallest useful Deep Agent with explicit task planning."""
    kwargs: dict[str, Any] = {
        "model": resolve_configured_model(model or "openai:gpt-5.5"),
        "tools": [probe_tool],
        "middleware": [TodoListMiddleware()],
        "system_prompt": SYSTEM_PROMPT,
    }
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if interrupt_on is not None:
        kwargs["interrupt_on"] = interrupt_on
    return create_deep_agent(**kwargs)


def invoke_with_thread(agent: Any, prompt: str, thread_id: str) -> Any:
    """Invoke an agent with the durable thread identifier required by LangGraph."""
    if not prompt.strip():
        raise ValueError("prompt must not be blank")
    if not thread_id.strip():
        raise ValueError("thread_id must not be blank")
    return agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        {"configurable": {"thread_id": thread_id}},
    )


def last_message_text(result: Any) -> str:
    """Extract final text for the CLI without exposing internal state."""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return ""
    message = messages[-1]
    if hasattr(message, "content"):
        content = message.content
    elif isinstance(message, Mapping):
        content = message.get("content", "")
    else:
        content = ""
    if isinstance(content, str):
        return content
    return str(content)
