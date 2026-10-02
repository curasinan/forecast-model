"""Thin OpenAI-compatible chat client for the local vLLM server, on httpx.

No retries (callers decide), no streaming (not needed for text -> structured
data). Structured output uses the server's ``response_format`` json_schema
mode; the decoded object is returned as ``LLMReply.parsed``. Reasoning text
(``reasoning_content``, produced by vLLM's qwen3 reasoning parser) is kept
separate from ``content`` so callers never accidentally parse it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import httpx

from src.llm.settings import LocalLLMSettings


class LLMError(RuntimeError):
    """Base class for every failure raised by LocalLLMClient."""


class LLMHTTPError(LLMError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class LLMOutputError(LLMError):
    """A schema was requested but the content is not valid JSON; ``raw`` keeps the text."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class LLMConfigError(LLMError):
    """LLM_MODEL does not match what the server serves."""


@dataclass
class LLMReply:
    content: str
    reasoning: str | None
    parsed: Any | None
    usage: dict[str, int]
    latency_s: float
    model: str


class LocalLLMClient:
    def __init__(
        self, settings: LocalLLMSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            transport=transport,
        )

    # -- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LocalLLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport --------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LLMError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"{method} {path}: non-JSON body: {response.text[:200]}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"{method} {path}: expected a JSON object, got {type(data).__name__}")
        return data

    # -- API ------------------------------------------------------------------ #

    def models(self) -> list[str]:
        """Ids the server lists under /models."""
        data = self._request("GET", "/models")
        return [str(item["id"]) for item in data.get("data", []) if item.get("id")]

    def assert_model_served(self) -> None:
        """Config over hardcode, made checkable: LLM_MODEL must be a served id."""
        served = self.models()
        if self.settings.model not in served:
            raise LLMConfigError(
                f"LLM_MODEL={self.settings.model!r} is not served; the server lists {served}"
            )

    def chat(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "reply",
        max_tokens: int | None = None,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens or self.settings.max_tokens,
            "reasoning_effort": self.settings.reasoning_effort,
        }
        if self.settings.temperature is not None:
            body["temperature"] = self.settings.temperature
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": dict(json_schema)},
            }
        started = time.perf_counter()
        data = self._request("POST", "/chat/completions", json=body)
        latency = time.perf_counter() - started
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed completion: {json.dumps(data)[:300]}") from exc
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content")
        usage_raw = data.get("usage") or {}
        usage = {k: int(v) for k, v in usage_raw.items() if isinstance(v, (int, float))}
        details = usage_raw.get("completion_tokens_details") or {}
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
            usage["reasoning_tokens"] = int(details["reasoning_tokens"])
        parsed: Any | None = None
        if json_schema is not None:
            try:
                parsed = json.loads(content)
            except ValueError as exc:
                raise LLMOutputError(f"content is not valid JSON: {exc}", raw=content) from exc
        return LLMReply(
            content=content,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            parsed=parsed,
            usage=usage,
            latency_s=latency,
            model=str(data.get("model", "")),
        )
