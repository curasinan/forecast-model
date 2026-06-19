"""Compare Metaculus list-endpoint vs detail-endpoint response shape.

The collector currently uses /api2/questions/ (list) and reads
``aggregations.recency_weighted.latest`` to extract the community
probability. If 100% of stored rows have null community_probability,
the most likely cause is one of:

  (A) aggregations is absent from the LIST response (very common — many
      APIs only return summary fields in lists, full payload in details);
  (B) the field path moved (renamed, nested differently);
  (C) the API requires a query-param like ``include=aggregations``.

This script fetches a single question via BOTH endpoints, dumps full
JSON to logs/, and reports which fields each response carries that
match commun/aggregat/predict/prob/forecast.

Run:
    $env:METACULUS_API_TOKEN = "<your_token>"
    python scripts/diagnose_metaculus.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

QUESTION_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 797


def main() -> int:
    token = os.environ.get("METACULUS_API_TOKEN", "").strip()
    if not token:
        print(
            "METACULUS_API_TOKEN is not set in the environment.\n"
            "PowerShell: $env:METACULUS_API_TOKEN = \"<token>\"  ; python scripts/diagnose_metaculus.py",
            file=sys.stderr,
        )
        return 1

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }

    # 1) DETAIL endpoint (single question, expected to be the most complete)
    detail_url = f"https://www.metaculus.com/api2/questions/{QUESTION_ID}/"
    print(f"[1] GET {detail_url}")
    rd = requests.get(detail_url, headers=headers, timeout=30)
    print(f"    -> HTTP {rd.status_code}")
    if rd.status_code != 200:
        print(f"    body: {rd.text[:500]}")
        return 1
    detail = rd.json()
    detail_path = LOGS_DIR / f"metaculus_q{QUESTION_ID}_detail.json"
    detail_path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    print(f"    saved -> {detail_path}  ({len(json.dumps(detail))} chars)")

    # 2) LIST endpoint (the one the collector uses), filtered to this single id if possible
    list_url = "https://www.metaculus.com/api2/questions/"
    print(f"\n[2] GET {list_url}?ids={QUESTION_ID}")
    rl = requests.get(list_url, headers=headers, params={"ids": QUESTION_ID}, timeout=30)
    print(f"    -> HTTP {rl.status_code}")
    if rl.status_code != 200:
        print(f"    body: {rl.text[:500]}")
        return 1
    list_payload = rl.json()
    list_results = list_payload.get("results") or []
    list_obj = list_results[0] if list_results else {}
    list_path = LOGS_DIR / f"metaculus_q{QUESTION_ID}_list.json"
    list_path.write_text(json.dumps(list_obj, indent=2), encoding="utf-8")
    print(f"    saved single-result obj -> {list_path}  ({len(json.dumps(list_obj))} chars)")

    # ---- Compare top-level keys --------------------------------------- #
    detail_keys = set(detail.keys())
    list_keys = set(list_obj.keys())
    print("\nTop-level keys in DETAIL but not LIST (likely the missing data):")
    for k in sorted(detail_keys - list_keys):
        print(f"  {k}")
    print("\nTop-level keys in LIST but not DETAIL:")
    for k in sorted(list_keys - detail_keys):
        print(f"  {k}")
    print("\nTop-level keys in BOTH:")
    for k in sorted(detail_keys & list_keys):
        print(f"  {k}")

    # ---- Find fields by name pattern --------------------------------- #
    interesting = ("commun", "aggregat", "predict", "prob", "forecast", "cp_reveal")

    def walk(obj: Any, path: str, hits: list[tuple[str, Any]]) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(t in k.lower() for t in interesting):
                    hits.append((f"{path}.{k}", v))
                walk(v, f"{path}.{k}", hits)
        elif isinstance(obj, list) and obj:
            walk(obj[0], f"{path}[0]", hits)

    print("\n" + "=" * 70)
    print("DETAIL response — fields matching commun/aggregat/predict/prob/forecast:")
    print("=" * 70)
    detail_hits: list[tuple[str, Any]] = []
    walk(detail, "", detail_hits)
    for path, val in detail_hits[:80]:
        s = repr(val)
        if len(s) > 240:
            s = s[:237] + "..."
        print(f"  {path}: {s}")

    print("\n" + "=" * 70)
    print("LIST-response single object — same field-name search:")
    print("=" * 70)
    list_hits: list[tuple[str, Any]] = []
    walk(list_obj, "", list_hits)
    if list_hits:
        for path, val in list_hits[:80]:
            s = repr(val)
            if len(s) > 240:
                s = s[:237] + "..."
            print(f"  {path}: {s}")
    else:
        print("  (no matches — the list endpoint probably omits aggregations entirely)")

    # ---- Drill down into aggregations specifically ------------------- #
    print("\n" + "=" * 70)
    print("Drill-down on detail['question']['aggregations'] (collector's path):")
    print("=" * 70)
    q = detail.get("question") if isinstance(detail.get("question"), dict) else detail
    aggs = q.get("aggregations") if isinstance(q, dict) else None
    if aggs is None:
        print("  detail['question']['aggregations'] is absent.")
        print("  Trying detail['aggregations'] (maybe top-level)...")
        aggs = detail.get("aggregations")
    if aggs is None:
        print("  detail['aggregations'] is also absent. Aggregations may live elsewhere entirely.")
    else:
        print(f"  aggregations keys: {list(aggs.keys()) if isinstance(aggs, dict) else type(aggs).__name__}")
        if isinstance(aggs, dict) and "recency_weighted" in aggs:
            rw = aggs["recency_weighted"]
            print(f"  recency_weighted keys: {list(rw.keys()) if isinstance(rw, dict) else type(rw).__name__}")
            if isinstance(rw, dict) and "latest" in rw:
                latest = rw["latest"]
                print(f"  recency_weighted.latest: {json.dumps(latest, indent=2)[:600]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
