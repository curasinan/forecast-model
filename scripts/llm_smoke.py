"""Is the local model up, and is LLM_MODEL what it actually serves?

    python scripts/llm_smoke.py                  # on the Spark
    ssh -L 8000:127.0.0.1:8000 spark             # from the Zenbook: tunnel first, then run

Exit 0 only when: the configured model is served, one schema-constrained
extraction round-trips as JSON, and the parsed object has every required
key. Prints latency, tokens/s and the reasoning-token count so a change in
serving flags (reasoning_effort, MTP) shows up here first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

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
    settings = LocalLLMSettings()
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
    missing = [k for k in SCHEMA["required"] if k not in (reply.parsed or {})]
    if missing:
        print(f"FAIL: missing keys {missing}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
