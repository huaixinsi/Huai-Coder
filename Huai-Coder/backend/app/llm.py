import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from .config import get_settings

SYSTEM_PROMPT = (
    "你是项目代码分析助手。只根据提供的项目上下文回答，不要输出 XML、DSML、tool_calls 或伪造的工具调用；"
    "如果上下文中没有足够信息，明确说明缺失内容。不要输出任何密钥、密码、Token 或凭证值。"
)


@dataclass
class ParsedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]  # original tool_call dict for message history
    argument_error: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_call: ParsedToolCall | None = None
    tool_calls: list[ParsedToolCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Keep the old single-call field source-compatible while exposing all
        # calls from providers that support parallel tool use.
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]
        elif self.tool_calls and self.tool_call is None:
            self.tool_call = self.tool_calls[0]


async def complete(prompt: str | list[dict], timeout: int = 60) -> str:
    """Backward-compatible completion. Accepts a string prompt or a messages list."""
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        text = prompt if isinstance(prompt, str) else str(prompt[-1].get("content", ""))
        return f"Received: {text}\n\nTry /list ., /read README.md, or /grep FastAPI backend"

    if isinstance(prompt, str):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    else:
        messages = prompt

    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    payload = {"model": settings.llm_model, "messages": messages, "stream": False}

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def complete_with_tools(
    messages: list[dict],
    tools: list[dict],
    timeout: int = 60,
) -> LLMResponse:
    """Completion with OpenAI-compatible function calling support.

    Returns LLMResponse with either .content (final answer) or .tool_call (action to take).
    """
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return LLMResponse(content="LLM not configured. Cannot execute tool calls.")

    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        choice = response.json()["choices"][0]["message"]

    # Parse every tool call in the assistant turn. Do not silently discard
    # additional calls: the ReAct scheduler decides whether they can run in
    # parallel or must be serialized.
    tool_calls = choice.get("tool_calls")
    if tool_calls:
        parsed_calls: list[ParsedToolCall] = []
        for index, tc in enumerate(tool_calls):
            fn = tc.get("function") or {}
            argument_error = None
            try:
                arguments = (
                    json.loads(fn.get("arguments", "{}"))
                    if isinstance(fn.get("arguments"), str)
                    else fn.get("arguments", {})
                )
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be a JSON object")
            except (json.JSONDecodeError, TypeError) as error:
                arguments = {}
                argument_error = str(error)
            parsed_calls.append(
                ParsedToolCall(
                    id=tc.get("id", f"call_{index}"),
                    name=fn.get("name", ""),
                    arguments=arguments,
                    raw=tc,
                    argument_error=argument_error,
                )
            )
        return LLMResponse(
            content=choice.get("content") or "",
            tool_calls=parsed_calls,
        )

    return LLMResponse(content=choice.get("content") or "")
