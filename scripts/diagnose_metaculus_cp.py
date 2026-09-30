"""Probe several Metaculus endpoints/parameters to find where the actual
community-prediction time series lives.

Standard /api2/questions/{id}/ returns aggregations.recency_weighted.latest=null
even on questions with hundreds of forecasts, so the field is shape-only.
This script tries several known patterns:

  1. /api2/questions/{id}/?with_cp=true
  2. /api2/questions/{id}/?include_aggregations=true
  3. /api2/questions/{id}/aggregations/
  4. /api2/questions/{id}/aggregations/?aggregation_method=recency_weighted
  5. /api/posts/{id}/  (newer URL root)
  6. /api/posts/{id}/?with_cp=true
  7. /api2/aggregation-explorer/?question_ids={id}
  8. /api2/aggregation_explorer/?question_ids={id}  (underscore vs hyphen)

Run:
    $env:METACULUS_API_TOKEN = "<token>"
    python scripts/diagnose_metaculus_cp.py [question_id]

Prints per-attempt: HTTP status, whether the response contains a non-null CP
value anywhere recognizable, and a small preview.
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


def find_non_null_cp(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """Walk a JSON object and collect paths where the leaf is a number AND
    the immediate ancestor key suggests it's a community-prediction value.
    """
    hits: list[tuple[str, Any]] = []

    def rec(o: Any, p: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower()
                # Likely CP value names
                if kl in ("centers", "interval_lower_bounds", "interval_upper_bounds",
                          "means", "medians", "center", "mean", "median",
                          "recency_weighted", "unweighted", "metaculus_prediction",
                          "community_prediction", "cp"):
                    hits.append((f"{p}.{k}", v))
                rec(v, f"{p}.{k}")
        elif isinstance(o, list) and o:
            rec(o[0], f"{p}[0]")

    rec(obj, path)
    return hits


def probe(label: str, method: str, url: str, params: dict[str, Any] | None,
          headers: dict[str, str], save_to: Path | None = None) -> None:
    print(f"\n--- {label} ---")
    print(f"{method} {url}  params={params}")
    try:
        r = requests.request(method, url, params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"    FAILED: {exc}")
        return
    print(f"    HTTP {r.status_code}  ({len(r.content)} bytes)")
    if r.status_code != 200:
        body_preview = r.text[:300] if r.text else ""
        print(f"    body preview: {body_preview!r}")
        return
    try:
        data = r.json()
    except json.JSONDecodeError:
        print(f"    not JSON. preview: {r.text[:300]!r}")
        return

    if save_to:
        save_to.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"    saved -> {save_to}")

    # If list-shaped, peek at the first result
    target = data
    if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
        n = len(data["results"])
        print(f"    list response with {n} results")
        if n:
            target = data["results"][0]

    hits = find_non_null_cp(target)
    non_null_hits = [(p, v) for p, v in hits if v not in (None, [], {})]
    if non_null_hits:
        print("    FOUND CP-like fields with non-null values:")
        for path, val in non_null_hits[:8]:
            preview = repr(val)
            if len(preview) > 160:
                preview = preview[:157] + "..."
            print(f"      {path}: {preview}")
    else:
        print("    no non-null CP-like fields detected")
        # Show the shape of recency_weighted if it exists, even if null
        def find_rw(o: Any) -> Any:
            if isinstance(o, dict):
                if "recency_weighted" in o:
                    return o["recency_weighted"]
                for v in o.values():
                    res = find_rw(v)
                    if res is not None:
                        return res
            elif isinstance(o, list) and o:
                return find_rw(o[0])
            return None
        rw = find_rw(target)
        if rw is not None:
            preview = json.dumps(rw)[:240]
            print(f"    recency_weighted shape (still null inside): {preview}")


def main() -> int:
    token = os.environ.get("METACULUS_API_TOKEN", "").strip()
    if not token:
        print("METACULUS_API_TOKEN not set.", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }

    qid = QUESTION_ID
    print(f"Probing community-prediction sources for question id={qid}\n")

    # 1. Detail endpoint with various flags
    base_detail = f"https://www.metaculus.com/api2/questions/{qid}/"
    probe("1) /api2/questions/{id}/  (no params, baseline)", "GET", base_detail, None, headers)
    probe("2) /api2/questions/{id}/?with_cp=true", "GET", base_detail, {"with_cp": "true"}, headers)
    probe("3) /api2/questions/{id}/?include_aggregations=true", "GET", base_detail, {"include_aggregations": "true"}, headers,
          save_to=LOGS_DIR / f"metaculus_q{qid}_detail_include_agg.json")
    probe("4) /api2/questions/{id}/?include_cp=true&include_aggregations=true", "GET", base_detail,
          {"include_cp": "true", "include_aggregations": "true"}, headers)

    # 2. Sub-resource endpoints that some Metaculus versions exposed
    probe("5) /api2/questions/{id}/aggregations/", "GET",
          f"https://www.metaculus.com/api2/questions/{qid}/aggregations/", None, headers,
          save_to=LOGS_DIR / f"metaculus_q{qid}_subresource_aggregations.json")
    probe("6) /api2/questions/{id}/aggregations/?aggregation_method=recency_weighted",
          "GET", f"https://www.metaculus.com/api2/questions/{qid}/aggregations/",
          {"aggregation_method": "recency_weighted"}, headers)
    probe("7) /api2/questions/{id}/prediction-history/", "GET",
          f"https://www.metaculus.com/api2/questions/{qid}/prediction-history/", None, headers)

    # 3. Newer /api/ root used by the Next.js frontend
    probe("8) /api/posts/{id}/", "GET",
          f"https://www.metaculus.com/api/posts/{qid}/", None, headers,
          save_to=LOGS_DIR / f"metaculus_post_{qid}.json")
    probe("9) /api/posts/{id}/?with_cp=true", "GET",
          f"https://www.metaculus.com/api/posts/{qid}/", {"with_cp": "true"}, headers,
          save_to=LOGS_DIR / f"metaculus_post_{qid}_with_cp.json")
    probe("10) /api/posts/{id}/aggregations/", "GET",
          f"https://www.metaculus.com/api/posts/{qid}/aggregations/", None, headers,
          save_to=LOGS_DIR / f"metaculus_post_{qid}_aggs.json")

    # 4. Aggregation explorer (mentioned as a Metaculus feature page)
    probe("11) /api2/aggregation-explorer/?question_ids={id}", "GET",
          "https://www.metaculus.com/api2/aggregation-explorer/", {"question_ids": qid}, headers)
    probe("12) /api2/aggregation_explorer/?question_ids={id}", "GET",
          "https://www.metaculus.com/api2/aggregation_explorer/", {"question_ids": qid}, headers)
    probe("13) /api/aggregation-explorer/?question_ids={id}", "GET",
          "https://www.metaculus.com/api/aggregation-explorer/", {"question_ids": qid}, headers,
          save_to=LOGS_DIR / f"metaculus_agg_explorer_{qid}.json")
    probe("14) /api/aggregation_explorer/?question_ids={id}", "GET",
          "https://www.metaculus.com/api/aggregation_explorer/", {"question_ids": qid}, headers)

    print("\n" + "=" * 70)
    print("Summary: any probe above that printed 'FOUND CP-like fields' is a")
    print("candidate. The smallest/cleanest one is the right endpoint to use.")
    print("Saved JSON files for the most informative attempts are in logs/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
