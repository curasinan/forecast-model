"""JSON-safe serialization helpers for MCP tool responses."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any


def json_safe(value: Any) -> Any:
    """Convert common Python values into JSON-serializable forms."""
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    # numpy / pandas scalars
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def ok(data: Any = None, **meta: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "error": None, "data": json_safe(data)}
    if meta:
        out["meta"] = json_safe(meta)
    return out


def err(code: str, message: str, *, data: Any = None, **meta: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": code,
        "message": message,
        "data": json_safe(data),
    }
    if meta:
        out["meta"] = json_safe(meta)
    return out


def clamp_limit(limit: int | None, default: int = 50, maximum: int = 500) -> int:
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))
