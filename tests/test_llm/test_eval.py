"""analysis/llm_eval.py and scripts/llm_eval.py: cases, scoring, the protocol gate, memory."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from analysis.cgroup_memory import GIB
from analysis.llm_eval import (
    ANY,
    CaseError,
    comparability_problems,
    load_cases,
    memory_summary,
    parse_nvidia_smi_apps,
    parse_vllm_log,
    protocol_problems,
    render_report,
    serve_flags,
    summarize,
    validate,
    values_equal,
)
from scripts.llm_eval import DEFAULT_CASES, main
from scripts.llm_smoke import PARAGRAPH, PROMPT
from scripts.llm_smoke import SCHEMA as SMOKE_SCHEMA
from tests.test_llm.test_real_shapes import FIXTURES, MESSAGES
from tests.test_llm.test_real_shapes import SCHEMA as FIXTURE_SCHEMA

MODEL = "Candidate-NVFP4"
EVAL_ARGS = ["--model", "/model", "--served-model-name", MODEL, "--tensor-parallel-size", "1",
             "--kv-cache-memory=4294967296", "--max-num-seqs", "8", "--max-model-len", "32768",
             "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
             "--reasoning-parser", "qwen3", "--host", "127.0.0.1", "--port", "8001"]
PRODUCTION_ARGS = ["--model", "/model", "--kv-cache-memory=10737418240",
                   "--max-model-len", "262144", "--enable-prefix-caching"]


def test_the_cases_file_loads_and_names_each_case_once():
    case_set = load_cases(DEFAULT_CASES)
    assert len(case_set.cases) >= 20
    assert len(case_set.sha256) == 64


def test_the_fixture_case_is_the_request_and_the_answer_the_capture_holds():
    case = {c.id: c for c in load_cases(DEFAULT_CASES).cases}["fixture-izmir"]
    captured = json.loads((FIXTURES / "vllm_chat_reply_schema.json").read_text(encoding="utf-8"))
    assert case.messages == MESSAGES and case.schema == FIXTURE_SCHEMA
    assert case.gold == json.loads(captured["choices"][0]["message"]["content"])


def test_the_smoke_case_is_the_smoke_scripts_request():
    case = {c.id: c for c in load_cases(DEFAULT_CASES).cases}["smoke-izmir"]
    assert case.schema == SMOKE_SCHEMA
    assert case.messages[-1]["content"] == f"{PROMPT}\n\n{PARAGRAPH}"
    assert case.gold == {"city": "Izmir", "year": 2024, "passed": True}


def _cases_file(tmp_path: Path, cases: list[dict], system: str | None = "Extract.") -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"system": system, "cases": cases}), encoding="utf-8")
    return path


def _case(**overrides) -> dict:
    case = {
        "id": "c1", "instruction": "Get the year.", "text": "It was 2024.",
        "schema": {"type": "object", "properties": {"year": {"type": "integer"}},
                   "required": ["year"], "additionalProperties": False},
        "gold": {"year": 2024},
    }
    case.update(overrides)
    return case


def test_instruction_and_text_become_a_system_and_a_user_message(tmp_path):
    (case,) = load_cases(_cases_file(tmp_path, [_case()])).cases
    assert case.messages == [{"role": "system", "content": "Extract."},
                             {"role": "user", "content": "Get the year.\n\nIt was 2024."}]
    assert case.schema_name == "extraction"


@pytest.mark.parametrize("cases, match", [
    ([_case(), _case()], "repeated"),
    ([_case(schema={"type": "object", "properties": {"year": {"type": "integer", "minimum": 0}},
                    "required": ["year"]})], "does not check"),
    ([_case(gold={"year": 2024, "extra": 1})], "exactly the schema's required keys"),
    ([_case(schema={"type": "object", "properties": {}, "required": ["year"]})],
     "without a property schema"),
    ([_case(gold={"year": "2024"})], "does not fit its own schema"),
    ([_case(gold={"year": {ANY: []}})], "non-empty list"),
    ([{k: v for k, v in _case().items() if k != "text"}], "either messages"),
])
def test_a_case_the_harness_cannot_score_honestly_is_refused(tmp_path, cases, match):
    with pytest.raises(CaseError, match=match):
        load_cases(_cases_file(tmp_path, cases))


def test_instruction_cases_need_the_system_prompt(tmp_path):
    with pytest.raises(CaseError, match="need 'system'"):
        load_cases(_cases_file(tmp_path, [_case()], system=None))


def test_validate_reads_the_schema_subset():
    schema = {"type": "object", "additionalProperties": False, "required": ["a", "b"],
              "properties": {"a": {"type": "integer"}, "b": {"type": ["number", "null"]},
                             "c": {"type": "array", "items": {"type": "string"}},
                             "d": {"type": "string", "enum": ["cut", "hold"]}}}
    assert validate({"a": 2024.0, "b": None, "c": ["x"], "d": "hold"}, schema) == []
    assert validate({"a": True, "b": 1}, schema) == ["$.a: expected integer, got bool"]
    assert validate({"a": 1}, schema) == ["$: missing b"]
    assert validate({"a": 1, "b": 1, "z": 0}, schema) == ["$: unexpected z"]
    assert validate({"a": 1, "b": 1, "c": [3]}, schema) == ["$.c[0]: expected string, got int"]
    assert validate({"a": 1, "b": 1, "d": "pause"}, schema)[0].startswith("$.d: 'pause'")
    assert validate([1], schema) == ["$: expected object, got list"]


@pytest.mark.parametrize("gold, got, equal", [
    ("Izmir", "İzmir ", True),
    ("Istanbul", "istanbul", True),
    ("BIST 100", "BIST100", False),
    ({ANY: ["BIST 100", "BIST100"]}, "BIST100", True),
    (38, 38.0, True),
    (-1.24, -1.2400000000001, True),
    (-1.24, 1.24, False),
    (1, True, False),
    (False, 0, False),
    (None, 0, False),
    (None, None, True),
    (["THYAO", "PGSUS"], ["pgsus", "thyao"], True),
    (["THYAO", "PGSUS"], ["THYAO", "THYAO"], False),
    (["THYAO", "PGSUS"], ["THYAO"], False),
    ({"a": 1}, {"a": 1.0}, True),
    ({"a": 1}, {"a": 1, "b": 2}, False),
])
def test_values_compare_as_the_cases_file_promises(gold, got, equal):
    assert values_equal(gold, got) is equal


def test_the_eval_containers_arguments_pass_and_productions_do_not():
    assert protocol_problems(EVAL_ARGS) == []
    problems = protocol_problems(PRODUCTION_ARGS)
    assert len(problems) == 2
    assert "10737418240" in problems[0] and "262144" in problems[1]


def test_a_missing_kv_flag_is_named_and_the_space_form_is_read():
    assert "no --kv-cache-memory" in protocol_problems(["--max-model-len", "32768"])[0]
    assert protocol_problems(["--kv-cache-memory", "4294967296", "--max-model-len=32768"]) == []


def test_a_flag_followed_by_a_flag_has_no_value():
    assert serve_flags(["--enable-prefix-caching", "--port", "8001", "--x=1"]) == {
        "enable-prefix-caching": None, "port": "8001", "x": "1"}


# ---------------------------------------------------------------- memory


def test_nvidia_smi_rows_sum_and_an_unknown_row_makes_the_whole_unknown():
    assert parse_nvidia_smi_apps("4242, 80000\n4243, 1200\n") == 81200
    assert parse_nvidia_smi_apps("4242, [N/A]\n") is None
    assert parse_nvidia_smi_apps("\n") is None


def test_the_vllm_log_gives_its_latest_numbers():
    log = ("Model loading took 74.30 GiB and 300 seconds\nGPU KV cache size: 1,000 tokens\n"
           "restart\nModel loading took 21.9GiB memory\nAvailable KV cache memory: 4.0 GiB\n"
           "GPU KV cache size: 131,072 tokens\n")
    assert parse_vllm_log(log) == {"weights_gib": 21.9, "kv_available_gib": 4.0,
                                   "kv_tokens": 131072.0}
    assert parse_vllm_log("") == {"weights_gib": None, "kv_available_gib": None,
                                  "kv_tokens": None}


def test_the_footprint_is_the_largest_drop_plus_the_hot_files():
    baseline = {"MemAvailable": 100 * GIB, "Cached": 10 * GIB, "Unevictable": GIB}
    snaps = {"loaded": {"MemAvailable": 70 * GIB, "Cached": 30 * GIB, "Unevictable": GIB},
             "after_cases": {"MemAvailable": 68 * GIB, "Cached": 30 * GIB,
                             "Unevictable": 3 * GIB}}
    memory = memory_summary(baseline, snaps, 34 * GIB)
    assert memory["held_bytes"] == 32 * GIB
    assert memory["page_cache_growth_bytes"] == 20 * GIB
    assert memory["unevictable_growth_bytes"] == 2 * GIB
    assert memory["footprint_bytes"] == 66 * GIB


def test_hot_files_that_got_locked_are_flagged_as_possibly_counted_twice(machine):
    machine.meminfo.write_text(f"MemAvailable: {70 * 1024 * 1024} kB\n"
                               f"Unevictable: {8 * 1024} kB\n", encoding="ascii")
    transport, _ = _server()
    run, _ = _docker()
    assert main(_run_args(machine), run=run, meminfo=machine.meminfo, transport=transport) == 0
    result = json.loads((machine.tmp / "cand.json").read_text(encoding="utf-8"))
    assert any("upper bound" in w for w in result["warnings"])


def test_summarize_counts_each_kind_of_outcome():
    def call(case, status, correct, total=2, wrong=(), tokens=40, latency=2.0):
        return {"case": case, "status": status, "fields_correct": correct, "fields_total": total,
                "wrong_fields": list(wrong), "completion_tokens": tokens, "latency_s": latency,
                "tok_s": tokens / latency if tokens is not None else None,
                "reasoning_tokens": 10, "finish_reason": "stop"}
    calls = [call("a", "ok", 2), call("b", "ok", 1, wrong=["x"]),
             call("c", "schema_invalid", 1, wrong=["y"]),
             call("d", "invalid_json", 0, wrong=["x", "y"], tokens=None)]
    summary = summarize(calls)
    assert summary["json_rate"] == 0.75 and summary["valid_json_rate"] == 0.5
    assert summary["field_accuracy"] == 4 / 8
    assert summary["exact_case_rate"] == 0.25
    assert summary["tok_s_median"] == 20.0 and summary["errors"] == 0
    assert summary["per_case"]["b"] == {"calls": 1, "exact": 0, "wrong_fields": {"x": 1}}


def _first(value):
    return value[ANY][0] if isinstance(value, dict) and set(value) == {ANY} else value


def _server(*, served=MODEL, wrong: dict | None = None, broken_json: str | None = None,
            http_500: str | None = None):
    """A vLLM stand-in that answers each case with its gold values, except where told."""
    by_messages = {json.dumps(c.messages): c for c in load_cases(DEFAULT_CASES).cases}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": served}]})
        body = json.loads(request.content)
        case = by_messages[json.dumps(body["messages"])]
        seen.append(case.id)
        if case.id == http_500:
            return httpx.Response(500, text="boom")
        answer = {key: _first(value) for key, value in case.gold.items()}
        answer.update((wrong or {}).get(case.id, {}))
        content = "{not json" if case.id == broken_json else json.dumps(answer)
        return httpx.Response(200, json={
            "model": served, "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": content, "reasoning": "r"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 40,
                      "completion_tokens_details": {"reasoning_tokens": 10}}})

    return httpx.MockTransport(handler), seen


def _docker(*, args=EVAL_ARGS, running=True, health="healthy"):
    calls: list[list[str]] = []
    inspect = [{"State": {"Running": running, "Health": {"Status": health}},
                "Config": {"Entrypoint": ["vllm", "serve"], "Cmd": args,
                           "Image": "vllm/vllm-openai:v0.31.0@sha256:c1c9"},
                "Image": "sha256:abc"}]

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(inspect), stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="",
                                   stderr="Model loading took 21.9 GiB and 40 s\n")
        if cmd[0] == "nvidia-smi":
            return SimpleNamespace(returncode=0, stdout="4242, 30000\n", stderr="")
        if cmd[0] == "git":
            out = "0123abcd\n" if "rev-parse" in cmd else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    return run, calls


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemAvailable: {100 * 1024 * 1024} kB\nCached: 0 kB\n", encoding="ascii")
    baseline = tmp_path / "baseline.json"
    assert main(["baseline", "--out", str(baseline)], meminfo=meminfo,
                port_open=lambda port: False) == 0
    meminfo.write_text(f"MemAvailable: {70 * 1024 * 1024} kB\nCached: {GIB // 1024} kB\n",
                       encoding="ascii")
    hot = tmp_path / "ples"
    hot.mkdir()
    (hot / "part0").write_bytes(b"x" * 4096)
    return SimpleNamespace(tmp=tmp_path, meminfo=meminfo, baseline=baseline, hot=hot)


def _run_args(machine, label="cand", out="cand.json", *extra):
    return ["run", "--label", label, "--model", MODEL, "--container", "eval-cand",
            "--baseline", str(machine.baseline), "--hot-dir", str(machine.hot),
            "--repeats", "2", "--out", str(machine.tmp / out), *extra]


def test_the_baseline_waits_until_no_llm_answers(tmp_path, capsys):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 1024 kB\n", encoding="ascii")
    out = tmp_path / "b.json"
    assert main(["baseline", "--out", str(out)], meminfo=meminfo,
                port_open=lambda port: port == 8001) == 2
    assert "[8001]" in capsys.readouterr().err and not out.exists()


def test_a_run_scores_every_case_and_measures_the_memory(machine):
    transport, seen = _server(wrong={"tcmb-cut-decimal-comma": {"new_pct": 3.8}},
                              broken_json="tickers-that-fell")
    run, _ = _docker()
    rc = main(_run_args(machine), run=run, meminfo=machine.meminfo, transport=transport)
    assert rc == 0
    result = json.loads((machine.tmp / "cand.json").read_text(encoding="utf-8"))
    n_cases = len(load_cases(DEFAULT_CASES).cases)
    assert len(seen) == 1 + 2 * n_cases  # the warm-up, then every case twice
    summary = result["summary"]
    assert summary["calls"] == 2 * n_cases and summary["errors"] == 0
    assert summary["valid_json_rate"] == (n_cases - 1) / n_cases
    assert summary["per_case"]["tcmb-cut-decimal-comma"]["wrong_fields"] == {"new_pct": 2}
    assert summary["per_case"]["tickers-that-fell"]["exact"] == 0
    memory = result["memory"]
    assert memory["held_bytes"] == 30 * GIB and memory["hot_files_bytes"] == 4096
    assert memory["nvidia_smi_apps_mib"] == 30000 and memory["vllm_log"]["weights_gib"] == 21.9
    assert result["protocol"] == {"kv_cache_bytes": 4 * GIB, "max_model_len": 32768}
    assert result["image"]["reference"].endswith("sha256:c1c9")
    assert result["git"] == {"sha": "0123abcd", "dirty": False}


@pytest.mark.parametrize("docker_kwargs, served, match", [
    ({"args": PRODUCTION_ARGS}, MODEL, "not served under the protocol"),
    ({"health": "starting"}, MODEL, "wait until it is healthy"),
    ({"running": False}, MODEL, "not running"),
    ({}, "Other-Model", "is not served"),
])
def test_a_candidate_outside_the_protocol_is_refused_before_any_case(
    machine, capsys, docker_kwargs, served, match
):
    transport, seen = _server(served=served)
    run, _ = _docker(**docker_kwargs)
    assert main(_run_args(machine), run=run, meminfo=machine.meminfo, transport=transport) == 2
    assert match in capsys.readouterr().err
    assert seen == [] and not (machine.tmp / "cand.json").exists()


def test_a_failed_request_marks_the_run_for_repeating(machine):
    transport, _ = _server(http_500="date-format")
    run, _ = _docker()
    assert main(_run_args(machine), run=run, meminfo=machine.meminfo, transport=transport) == 1
    result = json.loads((machine.tmp / "cand.json").read_text(encoding="utf-8"))
    assert result["summary"]["errors"] == 2


def test_the_report_compares_like_with_like_only(machine, capsys):
    for label in ("flash-next", "qwen36-35b"):
        transport, _ = _server()
        run, _ = _docker()
        assert main(_run_args(machine, label, f"{label}.json"), run=run,
                    meminfo=machine.meminfo, transport=transport) == 0
    results = [machine.tmp / "flash-next.json", machine.tmp / "qwen36-35b.json"]
    out = machine.tmp / "report.md"
    assert main(["report", *map(str, results), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| flash-next (`Candidate-NVFP4`) | 100.0% | 100.0% |" in text
    assert "max-model-len 32768" in text

    changed = json.loads(results[1].read_text(encoding="utf-8"))
    changed["repeats"] = 3
    results[1].write_text(json.dumps(changed), encoding="utf-8")
    capsys.readouterr()
    assert main(["report", *map(str, results)]) == 1
    assert "repeats differs" in capsys.readouterr().err


def test_comparability_refuses_a_baseline_among_the_results():
    assert comparability_problems([{"probe": "llm_eval_baseline"}])[0].startswith("not llm_eval")


def test_the_report_names_the_cases_a_candidate_missed(machine):
    transport, _ = _server(wrong={"relative-year": {"year": 2026}})
    run, _ = _docker()
    main(_run_args(machine), run=run, meminfo=machine.meminfo, transport=transport)
    result = json.loads((machine.tmp / "cand.json").read_text(encoding="utf-8"))
    assert "- cand: relative-year 0/2 (year)" in render_report([result])
