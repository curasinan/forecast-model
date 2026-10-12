"""Compare local models on the same cases: valid JSON, field accuracy, tokens/s and memory."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.cgroup_memory import GIB, parse_meminfo  # noqa: E402
from analysis.llm_eval import (  # noqa: E402
    PROTOCOL,
    CaseError,
    comparability_problems,
    directory_bytes,
    load_cases,
    memory_summary,
    parse_nvidia_smi_apps,
    parse_vllm_log,
    protocol_problems,
    render_report,
    run_cases,
    summarize,
)
from src.llm.client import LLMError, LLMOutputError, LocalLLMClient  # noqa: E402
from src.llm.settings import LocalLLMSettings  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "data" / "llm_eval_cases.json"
LLM_PORTS = (8000, 8001)
STALE_BASELINE_S = 12 * 3600


class Refused(RuntimeError):
    """A precondition of the run does not hold; nothing was measured."""


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _output(cmd: list[str], run, *, with_stderr: bool = False) -> str:
    try:
        done = run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"could not run {cmd[0]}: {type(exc).__name__}: {exc}") from exc
    if done.returncode != 0:
        raise Refused(f"{' '.join(cmd[:3])} exited {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout + (done.stderr or "") if with_stderr else done.stdout


def _container(name: str, run) -> dict[str, Any]:
    try:
        (info,) = json.loads(_output(["docker", "inspect", name], run))
    except ValueError as exc:
        raise Refused(f"docker inspect {name}: unreadable answer") from exc
    state = info.get("State", {})
    health = (state.get("Health") or {}).get("Status")
    if not state.get("Running"):
        raise Refused(f"container {name} is not running")
    if health not in (None, "healthy"):
        raise Refused(f"container {name} is {health}; wait until it is healthy")
    config = info.get("Config", {})
    return {
        "args": [*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])],
        "image": {"reference": config.get("Image"), "id": info.get("Image")},
    }


def _git(run) -> dict[str, Any]:
    try:
        sha = _output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], run).strip()
        dirty = bool(_output(["git", "-C", str(REPO_ROOT), "status", "--porcelain",
                              "--untracked-files=no"], run).strip())
    except Refused:
        return {"sha": None, "dirty": None}
    return {"sha": sha, "dirty": dirty}


def cmd_baseline(args, *, meminfo: Path, port_open) -> int:
    answering = [port for port in LLM_PORTS if port_open(port)]
    if answering:
        print(f"baseline: something answers on {answering}: stop every LLM first "
              "(cd ~/stack && docker compose stop qwenflash; and any eval container)",
              file=sys.stderr)
        return 2
    info = parse_meminfo(meminfo.read_text(encoding="ascii"))
    payload = {"probe": "llm_eval_baseline", "utc": _utc(), "epoch_s": time.time(),
               "host": platform.node(), "meminfo": info}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"baseline: MemAvailable {info['MemAvailable'] / GIB:.1f} GiB -> {args.out}")
    return 0


def cmd_run(args, *, run, meminfo: Path, transport, clock) -> int:
    warnings: list[str] = []
    try:
        case_set = load_cases(args.cases)
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline.get("probe") != "llm_eval_baseline":
            raise Refused(f"{args.baseline} is not a baseline from this script")
        container = _container(args.container, run)
        problems = protocol_problems(container["args"])
        if problems:
            raise Refused("not served under the protocol: " + "; ".join(problems))
        hot = {str(path): directory_bytes(path) for path in args.hot_dir}
    except (CaseError, Refused, OSError, ValueError) as exc:
        print(f"run: refused: {exc}", file=sys.stderr)
        return 2
    if time.time() - baseline.get("epoch_s", 0) > STALE_BASELINE_S:
        warnings.append("the baseline is more than 12 hours old")

    settings = LocalLLMSettings(
        _env_file=None, model=args.model, base_url=args.base_url, api_key="EMPTY",
        max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort,
        temperature=args.temperature, connect_timeout_s=10.0, read_timeout_s=args.read_timeout,
    )
    started = _utc()
    with LocalLLMClient(settings, transport=transport) as client:
        try:
            client.assert_model_served()
        except LLMError as exc:
            print(f"run: refused: {exc}", file=sys.stderr)
            return 2
        loaded = parse_meminfo(meminfo.read_text(encoding="ascii"))

        def chat(case):
            return client.chat(case.messages, json_schema=case.schema,
                               schema_name=case.schema_name)

        try:
            chat(case_set.cases[0])
        except LLMOutputError:
            pass
        except LLMError as exc:
            print(f"run: the warm-up request failed: {exc}", file=sys.stderr)
            return 1
        calls = run_cases(case_set, chat, repeats=args.repeats, output_error=LLMOutputError,
                          clock=clock)
        after = parse_meminfo(meminfo.read_text(encoding="ascii"))

    try:
        gpu_mib = parse_nvidia_smi_apps(_output(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"], run))
        if gpu_mib is None:
            warnings.append("nvidia-smi gave no per-process memory")
    except Refused as exc:
        gpu_mib = None
        warnings.append(f"nvidia-smi: {exc}")
    try:
        log = parse_vllm_log(_output(["docker", "logs", args.container], run,
                                       with_stderr=True))
    except Refused as exc:
        log = parse_vllm_log("")
        warnings.append(f"docker logs: {exc}")

    memory = memory_summary(baseline["meminfo"], {"loaded": loaded, "after_cases": after},
                            sum(hot.values()))
    memory.update(hot_dirs=hot, nvidia_smi_apps_mib=gpu_mib, vllm_log=log)
    if memory["hot_files_bytes"] and (memory["unevictable_growth_bytes"]
                                      >= memory["hot_files_bytes"] / 2):
        warnings.append(
            f"Unevictable grew by {memory['unevictable_growth_bytes'] / GIB:.1f} GiB: the hot "
            "files may be locked and already counted in 'held'; read the footprint as an upper "
            "bound")
    summary = summarize(calls)
    result = {
        "probe": "llm_eval", "version": 1, "utc_started": started, "utc_finished": _utc(),
        "host": platform.node(), "label": args.label, "model": args.model,
        "base_url": args.base_url, "container": args.container, "image": container["image"],
        "serve_args": container["args"], "protocol": PROTOCOL, "git": _git(run),
        "cases": {"path": str(args.cases), "sha256": case_set.sha256,
                  "count": len(case_set.cases)},
        "repeats": args.repeats,
        "settings": {"max_tokens": args.max_tokens, "reasoning_effort": args.reasoning_effort,
                     "temperature": args.temperature},
        "memory": memory, "summary": summary, "warnings": warnings, "calls": calls,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{args.label}: valid JSON {summary['valid_json_rate']:.1%}, field accuracy "
          f"{summary['field_accuracy']:.1%}, tok/s median {summary['tok_s_median']}, "
          f"footprint {memory['footprint_bytes'] / GIB:.1f} GiB, errors {summary['errors']}")
    print(f"result: {args.out}")
    for warning in warnings:
        print(f"warning: {warning}")
    return 1 if summary["errors"] else 0


def cmd_report(args) -> int:
    results = [json.loads(path.read_text(encoding="utf-8")) for path in args.results]
    problems = comparability_problems(results)
    if problems:
        print("report: the results are not comparable: " + "; ".join(problems), file=sys.stderr)
        return 1
    text = render_report(results)
    print(text, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None, *, run=subprocess.run,
         meminfo: Path = Path("/proc/meminfo"), port_open=_port_open,
         transport: httpx.BaseTransport | None = None, clock=time.perf_counter) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    base = sub.add_parser("baseline", help="memory with no LLM loaded")
    base.add_argument("--out", type=Path, required=True)

    one = sub.add_parser("run", help="one candidate, already up and healthy")
    one.add_argument("--label", required=True)
    one.add_argument("--model", required=True, help="the served model name")
    one.add_argument("--container", required=True)
    one.add_argument("--baseline", type=Path, required=True)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    one.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    one.add_argument("--repeats", type=int, default=3)
    one.add_argument("--hot-dir", type=Path, action="append", default=[],
                     help="files the model needs in page cache (Flash-Next's PLE sidecar)")
    one.add_argument("--max-tokens", type=int, default=8192)
    one.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    one.add_argument("--temperature", type=float, default=None)
    one.add_argument("--read-timeout", type=float, default=1800.0)

    rep = sub.add_parser("report", help="compare run results")
    rep.add_argument("results", type=Path, nargs="+")
    rep.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "baseline":
        return cmd_baseline(args, meminfo=meminfo, port_open=port_open)
    if args.command == "run":
        if args.repeats < 1:
            parser.error("--repeats must be at least 1")
        return cmd_run(args, run=run, meminfo=meminfo, transport=transport, clock=clock)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
