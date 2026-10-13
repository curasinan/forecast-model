"""Is the local model up, and is LLM_MODEL what it actually serves?"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.llm.client import LLMError, LocalLLMClient  # noqa: E402
from src.llm.settings import LocalLLMSettings  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "year": {"type": "integer"},
        "passed": {"type": "boolean"},
    },
    "required": ["city", "year", "passed"],
    "additionalProperties": False,
}
PARAGRAPH = (
    "In 2024 the city council of Izmir voted on extending the tram line to the "
    "university campus; the motion passed with 31 votes in favour."
)
PROMPT = (
    "Extract the city, the year, and whether the motion passed. "
    "Answer with a JSON object only."
)


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    try:
        settings = LocalLLMSettings()
    except ValidationError as exc:
        print(f"FAIL: settings: {exc}", file=sys.stderr)
        return 1
    print(f"base_url={settings.base_url} model={settings.model} effort={settings.reasoning_effort}")
    try:
        with LocalLLMClient(settings, transport=transport) as client:
            print("served:", client.models())
            client.assert_model_served()
            reply = client.chat(
                [
                    {"role": "system", "content": "You extract structured data from text."},
                    {"role": "user", "content": f"{PROMPT}\n\n{PARAGRAPH}"},
                ],
                json_schema=SCHEMA,
                schema_name="extraction",
            )
    except LLMError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    completion = reply.usage.get("completion_tokens", 0)
    rate = completion / reply.latency_s if reply.latency_s > 0 else 0.0
    print(
        f"latency {reply.latency_s:.1f}s, completion tokens {completion}, "
        f"{rate:.1f} tok/s, reasoning tokens {reply.usage.get('reasoning_tokens', 'n/a')}"
    )
    print("parsed:", reply.parsed)
    if not isinstance(reply.parsed, dict):
        print(
            f"FAIL: parsed reply is not a JSON object ({type(reply.parsed).__name__})",
            file=sys.stderr,
        )
        return 1
    missing = [k for k in SCHEMA["required"] if k not in reply.parsed]
    if missing:
        print(f"FAIL: missing keys {missing}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
