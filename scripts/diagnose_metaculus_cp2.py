"""Slow re-probe focused only on the endpoints that hit Cloudflare rate-limit last time."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

QUESTION_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 11620
DELAY_SECONDS = 15  # generous spacing to clear Cloudflare rate-limit


def deep_find_floats_under(obj: Any, target_keys: tuple[str, ...]) -> list[tuple[str, Any]]:
    """Find any path where a key matching target_keys holds a non-null, non-empty value."""
    hits: list[tuple[str, Any]] = []

    def rec(o: Any, p: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in target_keys and v not in (None, [], {}):
                    hits.append((f"{p}.{k}", v))
                rec(v, f"{p}.{k}")
        elif isinstance(o, list):
            if o:
                rec(o[0], f"{p}[0]")

    rec(obj, "")
    return hits


def probe(label: str, url: str, params: dict[str, Any] | None,
          headers: dict[str, str], save_to: Path) -> bool:
    """Returns True if this probe hit non-null CP-like data."""
    print(f"\n--- {label} ---")
    print(f"GET {url}  params={params}")
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"    FAILED: {exc}")
        return False

    print(f"    HTTP {r.status_code}  ({len(r.content)} bytes)")
    if r.status_code == 429:
        print("    RATE LIMITED — Cloudflare 1015. Wait longer between probes.")
        return False
    if r.status_code != 200:
        print(f"    body preview: {r.text[:240]!r}")
        return False
    try:
        data = r.json()
    except json.JSONDecodeError:
        print(f"    body preview (not JSON): {r.text[:240]!r}")
        return False

    save_to.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"    saved -> {save_to.name}  ({len(json.dumps(data))} chars)")

    target_keys = (
        "centers", "interval_lower_bounds", "interval_upper_bounds",
        "means", "medians", "history", "latest", "values", "y", "value",
    )
    hits = deep_find_floats_under(data, target_keys)
    if hits:
        print("    *** FOUND non-null CP-like fields ***")
        for path, val in hits[:6]:
            preview = repr(val)
            if len(preview) > 240:
                preview = preview[:237] + "..."
            print(f"      {path}: {preview}")
        return True
    print("    no non-null CP-like fields found")

    if isinstance(data, dict):
        print(f"    top-level keys: {sorted(data.keys())}")
    return False


def main() -> int:
    token = os.environ.get("METACULUS_API_TOKEN", "").strip()
    if not token:
        print("METACULUS_API_TOKEN not set.", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "User-Agent": "polymarket-trader/0.1 (+scripts.diagnose_metaculus_cp2)",
    }

    qid = QUESTION_ID
    print(f"Slow re-probe for question_id={qid}, sleeping {DELAY_SECONDS}s between requests")
    print(f"(Tip: pick a more recent / active question if {qid} returns nulls everywhere.)")

    probes = [
        ("A) /api/posts/{id}/aggregations/",
         f"https://www.metaculus.com/api/posts/{qid}/aggregations/", None,
         f"metaculus_post_{qid}_aggs.json"),
        ("B) /api/posts/{id}/aggregations/?aggregation_method=recency_weighted",
         f"https://www.metaculus.com/api/posts/{qid}/aggregations/",
         {"aggregation_method": "recency_weighted"},
         f"metaculus_post_{qid}_aggs_rw.json"),
        ("C) /api/aggregation-explorer/?question_ids={id}",
         "https://www.metaculus.com/api/aggregation-explorer/",
         {"question_ids": qid},
         f"metaculus_agg_explorer_{qid}.json"),
        ("D) /api/aggregation_explorer/?question_ids={id}",
         "https://www.metaculus.com/api/aggregation_explorer/",
         {"question_ids": qid},
         f"metaculus_agg_explorer_underscore_{qid}.json"),
        ("E) /api2/aggregation-explorer/?question_ids={id}",
         "https://www.metaculus.com/api2/aggregation-explorer/",
         {"question_ids": qid},
         f"metaculus_agg_explorer_api2_{qid}.json"),
        ("F) /api2/aggregation_explorer/?question_ids={id}",
         "https://www.metaculus.com/api2/aggregation_explorer/",
         {"question_ids": qid},
         f"metaculus_agg_explorer_api2_underscore_{qid}.json"),
    ]

    found_any = False
    for i, (label, url, params, fname) in enumerate(probes):
        if i > 0:
            print(f"\nSleeping {DELAY_SECONDS}s before next probe...")
            time.sleep(DELAY_SECONDS)
        if probe(label, url, params, headers, LOGS_DIR / fname):
            found_any = True

    print("\n" + "=" * 70)
    if found_any:
        print("DONE. At least one probe found CP-like data — see the *** FOUND lines.")
    else:
        print("DONE. No probe found CP-like data. Possible follow-ups:")
        print("  - Try a different question id (e.g., a very recent one)")
        print("  - Check if Metaculus moved the API behind a different auth scope")
        print("  - Inspect the saved JSONs for fields we didn't anticipate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
