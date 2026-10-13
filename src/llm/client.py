"""Thin OpenAI-compatible chat client for the local vLLM server, on httpx."""

from __future__ import annotations

import json
import math
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
    """LLM_MODEL does not match what the server serves, or LLM_BASE_URL is not a usable URL."""


_MISSING = object()


def _bad_shape(method: str, path: str, expected: str, got: object) -> LLMError:
    """The LLMError for an HTTP-200 payload of the wrong shape."""
    seen = "missing" if got is _MISSING else type(got).__name__
    return LLMError(f"{method} {path}: expected {expected}, got {seen}")


def _excerpt(data: object) -> str:
    """The first 300 characters of ``data`` as JSON, for an error message; never raises."""
    try:
        return json.dumps(data)[:300]
    except RecursionError:
        return "<nested too deeply to print>"


def _cutoff_note(finish_reason: str | None) -> str:
    """What an invalid-JSON error adds about how generation ended."""
    if finish_reason is None or finish_reason == "stop":
        return ""
    if finish_reason == "length":
        return " (finish_reason=length: the reply was cut off before it finished)"
    return f" (finish_reason={finish_reason})"


def _usage_count(value: Any, what: str) -> int:
    """A token count from a completion's ``usage``: an int or a finite float, truncated."""
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not is_number or (isinstance(value, float) and not math.isfinite(value)):
        raise _bad_shape("POST", "/chat/completions", f"{what} to be a finite number", value)
    return int(value)


@dataclass
class LLMReply:
    content: str
    reasoning: str | None
    parsed: Any | None
    usage: dict[str, int]
    latency_s: float
    model: str
    finish_reason: str | None = None


class LocalLLMClient:
    def __init__(
        self, settings: LocalLLMSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        timeout = httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s)
        try:
            self._http = httpx.Client(
                base_url=settings.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
                timeout=timeout,
                transport=transport,
                trust_env=False,
            )
        except httpx.InvalidURL as exc:
            raise LLMConfigError(f"LLM_BASE_URL cannot be used: {exc}") from exc

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LocalLLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LLMError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        try:
            data = response.json()
        except (ValueError, RecursionError) as exc:  # RecursionError: nested past the decoder
            raise LLMError(f"{method} {path}: non-JSON body: {response.text[:200]}") from exc
        if not isinstance(data, dict):
            raise _bad_shape(method, path, "a JSON object", data)
        return data

    def models(self) -> list[str]:
        """Ids the server lists under /models."""
        data = self._request("GET", "/models")
        entries = data.get("data", _MISSING)  # {} is a broken server, not an empty listing
        if not isinstance(entries, list):
            raise _bad_shape("GET", "/models", "a list under 'data'", entries)
        ids: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise _bad_shape("GET", "/models", "each 'data' entry to be an object", entry)
            model_id = entry.get("id")
            if not isinstance(model_id, str):
                raise _bad_shape(
                    "GET", "/models", "each 'data' entry to have a string 'id'", model_id
                )
            ids.append(model_id)
        return ids

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
            "max_tokens": max_tokens if max_tokens is not None else self.settings.max_tokens,
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
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed completion: {_excerpt(data)}") from exc
        if not isinstance(message, dict):
            raise _bad_shape("POST", "/chat/completions", "'message' to be an object", message)
        content = message.get("content")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            raise _bad_shape(
                "POST", "/chat/completions", "'content' to be a string or null", content
            )
        reasoning_key = (
            "reasoning_content" if message.get("reasoning_content") is not None else "reasoning"
        )
        reasoning = message.get(reasoning_key)
        if reasoning is not None and not isinstance(reasoning, str):
            raise _bad_shape(
                "POST", "/chat/completions", f"'{reasoning_key}' to be a string or null", reasoning
            )
        usage_raw = data.get("usage")
        if usage_raw is None:
            usage_raw = {}
        elif not isinstance(usage_raw, dict):
            raise _bad_shape("POST", "/chat/completions", "'usage' to be an object", usage_raw)
        usage: dict[str, int] = {}
        for key, value in usage_raw.items():
            if value is None or isinstance(value, dict):
                continue
            usage[key] = _usage_count(value, "each 'usage' count")
        details = usage_raw.get("completion_tokens_details")
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            usage["reasoning_tokens"] = _usage_count(
                details["reasoning_tokens"], "'reasoning_tokens'"
            )
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise _bad_shape(
                "POST", "/chat/completions", "'finish_reason' to be a string or null", finish_reason
            )
        parsed: Any | None = None
        if json_schema is not None:
            try:
                parsed = json.loads(content)
            except (ValueError, RecursionError) as exc:
                raise LLMOutputError(
                    f"content is not valid JSON{_cutoff_note(finish_reason)}: {exc}", raw=content
                ) from exc
        return LLMReply(
            content=content,
            reasoning=reasoning,
            parsed=parsed,
            usage=usage,
            latency_s=latency,
            model=str(data.get("model", "")),
            finish_reason=finish_reason,
        )
