"""Comparing local models on the same extraction cases: the library of scripts/llm_eval.py."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis.cgroup_memory import GIB

PROTOCOL = {"kv_cache_bytes": 4 * GIB, "max_model_len": 32768}
KV_FLAGS = ("kv-cache-memory", "kv-cache-memory-bytes")
SCHEMA_KEYWORDS = {"type", "properties", "required", "additionalProperties", "enum", "items"}
ANY = "$any"


class CaseError(ValueError):
    """The cases file cannot be used as it is."""


# ---------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    id: str
    messages: list[dict[str, str]]
    schema: dict[str, Any]
    schema_name: str
    gold: dict[str, Any]


@dataclass(frozen=True)
class CaseSet:
    path: str
    sha256: str
    cases: list[Case]


def _check_schema_keywords(schema: Any, where: str) -> None:
    if not isinstance(schema, dict):
        raise CaseError(f"{where}: a schema must be an object")
    unknown = set(schema) - SCHEMA_KEYWORDS
    if unknown:
        raise CaseError(f"{where}: schema keywords this harness does not check: {sorted(unknown)}")
    for name, sub in schema.get("properties", {}).items():
        _check_schema_keywords(sub, f"{where}.{name}")
    if "items" in schema:
        _check_schema_keywords(schema["items"], f"{where}[]")


def _gold_alternatives(value: Any) -> list[Any]:
    if isinstance(value, dict) and set(value) == {ANY}:
        alternatives = value[ANY]
        if not isinstance(alternatives, list) or not alternatives:
            raise CaseError(f"{ANY} needs a non-empty list, got {alternatives!r}")
        return alternatives
    return [value]


def load_cases(path: Path) -> CaseSet:
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CaseError(f"{path}: not JSON: {exc}") from exc
    system = data.get("system")
    entries = data.get("cases")
    if not isinstance(entries, list) or not entries:
        raise CaseError(f"{path}: no cases")
    cases: list[Case] = []
    seen: set[str] = set()
    for entry in entries:
        case_id = entry.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise CaseError(f"{path}: a case id is missing or repeated: {case_id!r}")
        seen.add(case_id)
        schema, gold = entry.get("schema"), entry.get("gold")
        _check_schema_keywords(schema, case_id)
        if schema.get("type") != "object" or not isinstance(gold, dict):
            raise CaseError(f"{case_id}: the schema must describe an object and gold be one")
        if sorted(schema.get("required", [])) != sorted(gold):
            raise CaseError(f"{case_id}: gold keys must be exactly the schema's required keys")
        undescribed = sorted(set(gold) - set(schema.get("properties", {})))
        if undescribed:
            raise CaseError(f"{case_id}: required keys without a property schema: {undescribed}")
        for key, value in gold.items():
            for alternative in _gold_alternatives(value):
                problems = validate(alternative, schema["properties"][key], f"$.{key}")
                if problems:
                    raise CaseError(f"{case_id}: gold does not fit its own schema: {problems}")
        if "messages" in entry:
            messages = entry["messages"]
        elif isinstance(entry.get("instruction"), str) and isinstance(entry.get("text"), str):
            if not isinstance(system, str):
                raise CaseError(f"{path}: cases built from instruction and text need 'system'")
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{entry['instruction']}\n\n{entry['text']}"},
            ]
        else:
            raise CaseError(f"{case_id}: give either messages, or instruction and text")
        cases.append(Case(case_id, messages, schema, entry.get("schema_name", "extraction"), gold))
    return CaseSet(str(path), hashlib.sha256(raw).hexdigest(), cases)


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if name == "number":
        return math.isfinite(value)
    if name == "integer":
        return isinstance(value, int) or (math.isfinite(value) and float(value).is_integer())
    raise CaseError(f"unknown schema type {name!r}")


def validate(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Problems of ``value`` against the schema subset in SCHEMA_KEYWORDS; [] when it fits."""
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_is_type(value, name) for name in names):
            return [f"{path}: expected {types}, got {type(value).__name__}"]
    if "enum" in schema and not any(
        type(value) is type(option) and value == option for option in schema["enum"]
    ):
        return [f"{path}: {value!r} is not one of {schema['enum']}"]
    problems: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems += [f"{path}: missing {key}" for key in schema.get("required", [])
                     if key not in value]
        if schema.get("additionalProperties") is False:
            problems += [f"{path}: unexpected {key}" for key in value if key not in properties]
        for key, item in value.items():
            if key in properties:
                problems += validate(item, properties[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems += validate(item, schema["items"], f"{path}[{index}]")
    return problems


def normalize_text(text: str) -> str:
    """Case, accents and spacing removed: "İzmir " and "izmir" compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def values_equal(gold: Any, got: Any) -> bool:
    if isinstance(gold, dict) and set(gold) == {ANY}:
        return any(values_equal(alternative, got) for alternative in gold[ANY])
    if gold is None or isinstance(gold, bool):
        return got is gold
    if isinstance(gold, (int, float)):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        return math.isclose(gold, got, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(gold, str):
        return isinstance(got, str) and normalize_text(gold) == normalize_text(got)
    if isinstance(gold, list):  # in any order, each element used once
        if not isinstance(got, list) or len(got) != len(gold):
            return False
        unused = list(got)
        for item in gold:
            match = next((i for i, other in enumerate(unused) if values_equal(item, other)), None)
            if match is None:
                return False
            unused.pop(match)
        return True
    if isinstance(gold, dict):
        return (isinstance(got, dict) and set(got) == set(gold)
                and all(values_equal(gold[key], got[key]) for key in gold))
    raise CaseError(f"gold value of an unsupported kind: {gold!r}")


def score_fields(gold: Mapping[str, Any], parsed: Any) -> tuple[int, list[str]]:
    """(fields right, names of fields wrong or missing); a non-object reply gets nothing right."""
    if not isinstance(parsed, dict):
        return 0, sorted(gold)
    wrong = sorted(key for key in gold if key not in parsed
                   or not values_equal(gold[key], parsed[key]))
    return len(gold) - len(wrong), wrong


def serve_flags(args: list[str]) -> dict[str, str | None]:
    """``--name value`` and ``--name=value`` pairs; a flag followed by a flag has no value."""
    flags: dict[str, str | None] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            name, sep, value = token[2:].partition("=")
            if sep:
                flags[name] = value
            elif index + 1 < len(args) and not args[index + 1].startswith("--"):
                flags[name] = args[index + 1]
                index += 1
            else:
                flags[name] = None
        index += 1
    return flags


def protocol_problems(args: list[str]) -> list[str]:
    flags = serve_flags(args)
    problems: list[str] = []
    kv_values = [flags[name] for name in KV_FLAGS if name in flags]
    if not kv_values:
        problems.append("no --kv-cache-memory: vLLM would size the KV cache from "
                        "--gpu-memory-utilization, a share of the whole unified memory")
    elif any(value != str(PROTOCOL["kv_cache_bytes"]) for value in kv_values):
        problems.append(f"--kv-cache-memory is {kv_values}, the protocol's is "
                        f"{PROTOCOL['kv_cache_bytes']} (4 GiB)")
    if flags.get("max-model-len") != str(PROTOCOL["max_model_len"]):
        problems.append(f"--max-model-len is {flags.get('max-model-len')!r}, the protocol's is "
                        f"{PROTOCOL['max_model_len']}")
    return problems


# ---------------------------------------------------------------- memory


def parse_nvidia_smi_apps(text: str) -> int | None:
    """Sum of used_memory (MiB) over ``--query-compute-apps=pid,used_memory`` csv rows."""
    total = 0
    rows = [line for line in text.splitlines() if line.strip()]
    if not rows:
        return None
    for line in rows:
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2 or not fields[1].isdigit():
            return None
        total += int(fields[1])
    return total


LOG_PATTERNS = {
    "weights_gib": re.compile(r"Model loading took ([0-9.]+) ?GiB"),
    "kv_available_gib": re.compile(r"Available KV cache memory: ([0-9.]+) ?GiB"),
    "kv_tokens": re.compile(r"GPU KV cache size: ([0-9,]+) tokens"),
}


def parse_vllm_log(text: str) -> dict[str, float | None]:
    """The last value of each LOG_PATTERNS line in a container's log (its latest start)."""
    found: dict[str, float | None] = {}
    for name, pattern in LOG_PATTERNS.items():
        matches = pattern.findall(text)
        found[name] = float(matches[-1].replace(",", "")) if matches else None
    return found


def directory_bytes(path: Path) -> int:
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not a directory")
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def memory_summary(
    baseline: Mapping[str, int], snapshots: Mapping[str, Mapping[str, int]], hot_bytes: int
) -> dict[str, Any]:
    held = max(baseline["MemAvailable"] - snap["MemAvailable"] for snap in snapshots.values())
    cache = max(snap.get("Cached", 0) - baseline.get("Cached", 0) for snap in snapshots.values())
    locked = max(snap.get("Unevictable", 0) - baseline.get("Unevictable", 0)
                 for snap in snapshots.values())
    return {
        "held_bytes": held,
        "page_cache_growth_bytes": cache,
        "unevictable_growth_bytes": locked,
        "hot_files_bytes": hot_bytes,
        "footprint_bytes": held + hot_bytes,
        "baseline_available_bytes": baseline["MemAvailable"],
        "snapshots": {name: {key: snap.get(key) for key in
                             ("MemAvailable", "MemFree", "Cached", "Mlocked", "Unevictable")}
                      for name, snap in snapshots.items()},
    }


def _call_record(case: Case, repeat: int, chat: Callable[[Case], Any],
                 output_error: type[Exception], clock: Callable[[], float]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "case": case.id, "repeat": repeat, "status": "ok", "error": None,
        "finish_reason": None, "latency_s": None, "completion_tokens": None,
        "reasoning_tokens": None, "tok_s": None, "fields_total": len(case.gold),
        "fields_correct": 0, "wrong_fields": sorted(case.gold), "parsed": None, "raw": None,
    }
    started = clock()
    try:
        reply = chat(case)
    except output_error as exc:
        record.update(status="invalid_json", error=str(exc)[:300],
                      raw=getattr(exc, "raw", "")[:500], latency_s=clock() - started)
        return record
    except Exception as exc:
        record.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300],
                      latency_s=clock() - started)
        return record
    completion = reply.usage.get("completion_tokens", 0)
    record.update(
        finish_reason=reply.finish_reason, latency_s=reply.latency_s,
        completion_tokens=completion, reasoning_tokens=reply.usage.get("reasoning_tokens"),
        tok_s=completion / reply.latency_s if reply.latency_s > 0 else None,
        parsed=reply.parsed,
    )
    problems = validate(reply.parsed, case.schema)
    if problems:
        record.update(status="schema_invalid", error="; ".join(problems)[:300],
                      raw=reply.content[:500])
    record["fields_correct"], record["wrong_fields"] = score_fields(case.gold, reply.parsed)
    return record


def run_cases(case_set: CaseSet, chat: Callable[[Case], Any], *, repeats: int,
              output_error: type[Exception],
              clock: Callable[[], float] = time.perf_counter) -> list[dict[str, Any]]:
    """Every case ``repeats`` times, a whole pass at a time so drift spreads over all cases."""
    return [_call_record(case, repeat, chat, output_error, clock)
            for repeat in range(1, repeats + 1) for case in case_set.cases]


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def summarize(calls: list[dict[str, Any]]) -> dict[str, Any]:
    answered = [c for c in calls if c["status"] != "error"]
    timed = [c for c in answered if c["completion_tokens"] is not None and c["latency_s"]]
    rates = [c["tok_s"] for c in timed if c["tok_s"] is not None]
    reasoning = [c["reasoning_tokens"] for c in timed if c["reasoning_tokens"] is not None]
    fields_total = sum(c["fields_total"] for c in calls)
    per_case: dict[str, dict[str, Any]] = {}
    for call in calls:
        entry = per_case.setdefault(call["case"], {"calls": 0, "exact": 0, "wrong": Counter()})
        entry["calls"] += 1
        entry["exact"] += call["status"] == "ok" and not call["wrong_fields"]
        entry["wrong"].update(call["wrong_fields"])
    n = len(calls)
    return {
        "calls": n,
        "errors": n - len(answered),
        "json_rate": sum(c["status"] in ("ok", "schema_invalid") for c in calls) / n,
        "valid_json_rate": sum(c["status"] == "ok" for c in calls) / n,
        "field_accuracy": sum(c["fields_correct"] for c in calls) / fields_total,
        "exact_case_rate": sum(e["exact"] for e in per_case.values()) / n,
        "tok_s_median": statistics.median(rates) if rates else None,
        "tok_s_aggregate": (sum(c["completion_tokens"] for c in timed)
                            / sum(c["latency_s"] for c in timed)) if timed else None,
        "latency_median_s": statistics.median(c["latency_s"] for c in timed) if timed else None,
        "latency_p90_s": _p90([c["latency_s"] for c in timed]) if timed else None,
        "reasoning_tokens_median": statistics.median(reasoning) if reasoning else None,
        "finish_reasons": dict(Counter(str(c["finish_reason"]) for c in answered)),
        "per_case": {case_id: {"calls": e["calls"], "exact": e["exact"],
                               "wrong_fields": dict(e["wrong"])}
                     for case_id, e in per_case.items()},
    }


def comparability_problems(results: list[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    not_runs = [r.get("label", "?") for r in results if r.get("probe") != "llm_eval"]
    if not_runs:
        return [f"not llm_eval run results: {not_runs}"]
    labels = [r["label"] for r in results]
    if len(set(labels)) != len(labels):
        problems.append(f"labels repeat: {labels}")
    for key in ("cases.sha256", "repeats", "protocol", "settings"):
        values = {json.dumps(_dig(r, key), sort_keys=True) for r in results}
        if len(values) > 1:
            problems.append(f"{key} differs between the results")
    return problems


def _dig(data: Mapping[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        data = data[part]
    return data


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(results: list[Mapping[str, Any]]) -> str:
    first = results[0]
    lines = [
        "# Local model eval",
        "",
        f"Cases: `{first['cases']['path']}` (sha256 `{first['cases']['sha256'][:12]}`, "
        f"{first['cases']['count']} cases x {first['repeats']} repeats). Protocol: KV cache "
        f"{first['protocol']['kv_cache_bytes'] // GIB} GiB, max-model-len "
        f"{first['protocol']['max_model_len']}. Settings: {json.dumps(first['settings'])}.",
        "",
        "| Candidate | Valid JSON | Field accuracy | Exact cases | tok/s median | "
        "Latency median (s) | Memory held (GiB) | + hot files (GiB) | Footprint (GiB) | "
        "Weights in log (GiB) | Cut off (length) | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s, m = r["summary"], r["memory"]
        lines.append(
            f"| {r['label']} (`{r['model']}`) | {_pct(s['valid_json_rate'])} | "
            f"{_pct(s['field_accuracy'])} | {_pct(s['exact_case_rate'])} | "
            f"{_num(s['tok_s_median'])} | {_num(s['latency_median_s'])} | "
            f"{_num(m['held_bytes'] / GIB)} | {_num(m['hot_files_bytes'] / GIB)} | "
            f"{_num(m['footprint_bytes'] / GIB)} | {_num(m['vllm_log']['weights_gib'])} | "
            f"{s['finish_reasons'].get('length', 0)} | {s['errors']} |"
        )
    lines += ["", "Cases a candidate did not get exactly right every time:", ""]
    for r in results:
        misses = {case_id: entry for case_id, entry in r["summary"]["per_case"].items()
                  if entry["exact"] < entry["calls"]}
        detail = "; ".join(f"{case_id} {e['exact']}/{e['calls']} ({', '.join(e['wrong_fields'])})"
                           for case_id, e in sorted(misses.items())) or "none"
        lines.append(f"- {r['label']}: {detail}")
    warnings = [f"{r['label']}: {w}" for r in results for w in r.get("warnings", [])]
    if warnings:
        lines += ["", "Warnings:", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines) + "\n"
