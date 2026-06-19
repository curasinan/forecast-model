# API findings — Step 0 probe

**Probe run:** 2026-05-06, from `~/OneDrive/Desktop/new_work` against live endpoints.

## Status overview

| Source | Endpoint(s) tried | Result |
|---|---|---|
| Metaculus | `/api2/questions/`, `/api/posts/`, `/api/questions/`, `/api2/categories/`, `/api/categories/`, `/api2/questions/{id}/`, `/api/posts/{id}/` (with descriptive User-Agent, no token) | **403 Permission Error on every endpoint.** Body: `"Permission Error: The API is only available to authenticated users. Please create an account and use your API token to access the API."` |
| Polymarket (Gamma) | Already documented; not re-probed. Uses `/events?closed=false&order=id&ascending=false` for the markets feed. Field shape captured by existing `collectors/collect_markets.py:74-80`. |
| Polymarket CLOB | Already integrated via `collectors/collect_snapshots.py`; reads `get_order_book(token_id)` returning `OrderBookSummary(bids, asks, last_trade_price, ...)`. Field shape captured in that module. |

## Polymarket shapes (from existing project)

### `markets` table (existing DB at `data/polymarket.db`, 187,964 rows)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT (PK) | Polymarket internal market id |
| `question` | TEXT | Human-readable resolution question |
| `condition_id` | TEXT | On-chain condition |
| `slug` | TEXT | URL slug |
| `clob_token_ids` | TEXT (JSON array) | YES/NO outcome token IDs |
| `end_date` | TEXT (ISO 8601) | Scheduled resolution time |
| `volume` | REAL | Lifetime USD volume |
| `volume_24h` | REAL | Trailing 24h USD volume |
| `liquidity` | REAL | Current order-book depth proxy |
| `status` | TEXT | `'active'` / `'resolved_or_removed'` |
| `resolution` | TEXT (nullable) | Free-form resolution string when resolved |
| `created_at` | TEXT (ISO 8601) | First-seen-by-collector timestamp |
| `last_updated` | TEXT (ISO 8601) | Last refresh from Gamma |
| `orderbook_missing` | INTEGER (bool) | Set when CLOB returns 404 for the token |

### `snapshots` table (241,157 rows, 15-min cadence)

| Column | Type |
|---|---|
| `id` | INTEGER (autoinc PK) |
| `market_id` | TEXT (FK → markets.id) |
| `timestamp` | TEXT (ISO 8601 UTC) |
| `best_bid` / `best_ask` | REAL |
| `bid_depth` / `ask_depth` | REAL |
| `midpoint` | REAL |
| `spread` | REAL |
| `last_price` | REAL |
| `volume_24h` | REAL |

Unique constraint on `(market_id, timestamp)`; index on `market_id`.

## Metaculus probe details

Command:
```
curl -A "trade_bot/0.1 (api_findings probe; contact: dev@example.com)" \
  https://www.metaculus.com/api2/questions/?status=open&forecast_type=binary&limit=2
```

Response: `HTTP 403`, body:
```
Permission Error: The API is only available to authenticated users.
Please create an account and use your API token to access the API.
```

Same response for every variant tried (`/api/`, `/api2/`, `/api/posts/`, `/api2/categories/`, single-question lookup). No public/anonymous read access surface available.

**Implication:** The pipeline cannot fetch from Metaculus without an API token. The spec's env var list (`METACULUS_USER_AGENT`, `DB_PATH`, `LOG_LEVEL`) is missing `METACULUS_API_TOKEN` (or equivalent).

The `tests/fixtures/metaculus_sample.json` file currently contains the 403 error body — it is NOT a usable schema fixture. Will be overwritten with a real successful response once auth is resolved.

## Open blockers

1. Need a Metaculus API token (or alternative data source) before any meaningful schema work on `MetaculusQuestion` can begin.
2. Without a real sample response, the actual JSON path to the binary community-prediction median (the spec mentioned `community_prediction.full.q2`) cannot be verified — and the spec's "fail loud on missing field" constraint means I shouldn't guess.
