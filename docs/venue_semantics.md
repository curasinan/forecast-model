# IBKR ForecastEx venue semantics (empirically verified facts only)

Rules for this file:

- Every entry must state the **claim**, the **evidence** (which probe/report
  produced it), the **date verified**, and the **invalidation condition**
  (what would make it stale).
- Nothing goes in here from documentation, marketing pages, or this
  migration's spec text alone. `scripts/verify_ibkr_entitlements.py` reports
  and live API responses are the only admissible sources.
- Documentation and third-party summaries about this venue have proven
  inconsistent; when a doc claim and an API observation disagree, the API
  observation wins and the disagreement is worth recording.

Status: **Stage 0 ran 2026-08-16 against the LIVE session (exit 0).**
Reports: `docs/stage0_report_2026-08-16_live.md` (+ `_followup`).
Universe snapshot: `data/ibkr_universe_live.json`.
Still open: legal entity confirmation, coupon rate, historical bars,
order-size/position limits, paper parity.

## The 8 questions from the migration spec

### 1. Which IBKR entity holds the account, and which products it may access

- Claim: account `U0000000`, type INDIVIDUAL, base currency USD, live
  (U-prefix). Legal entity NOT yet confirmed — the API does not expose it;
  user must read it off Client Portal / statements.
- Evidence: Stage 0 report section 1 (accountSummary), 2026-08-16.
- Date verified: 2026-08-16 (partial)
- Invalidation: account migration between IBKR entities; permission changes.

### 2. Are Yes and No separate contracts or two sides of one?

- Claim: **SEPARATE CONTRACTS.** Tradable event contracts are `secType='OPT'`
  on exchange `FORECASTX`; every (symbol, expiry, strike) group contains
  exactly 2 conIds; `right='C'` is the YES leg and `right='P'` the NO leg;
  `localSymbol` encodes it (`ACD_1226_432.0_YES` / `ACD_1226_432.0_NO`).
  The quoted `symbol` (GDP, UNR, CPI, …) is a non-tradable `secType='IND'`
  reference index (orders on it are rejected with error 10345).
- Evidence: Stage 0 report sections 2–4; sample conIds 712856754/712856759.
- Date verified: 2026-08-16
- Invalidation: venue restructures contract listing.

### 3. Which order types are supported; are resting limit orders permitted?

- Claim (venue-declared, per contract metadata): tradable contracts declare
  `LMT, MKT, STP, STPLMT, IOC, GTC, GTD, GTT, TRAIL, MIT, LIT, MTL, SNAPMID,
  SNAPMKT, SNAPREL, PEGBENCH, OCA, COND/CONDORDER, SCALE, HID, ALGO, …`
  GTC in the list implies resting limit orders are permitted.
- whatIf confirmation: **ACCEPTED for LMT DAY, LMT GTC, MKT DAY, and
  LMT+PriceCondition** (explicit tif required — see discovery mechanics).
  **Resting limit orders (GTC) are permitted.**
- Evidence: `orderTypes` field of conId 712856754 (Stage 0 section 5);
  whatIf acceptances in `docs/stage0_report_2026-08-16_live_followup.md`.
- Date verified: 2026-08-16
- Invalidation: exchange rule change; new TWS API version.

### 4. Native order book with depth, or top of book only?

- Claim: a depth feed EXISTS — `reqMktDepthExchanges` lists FORECASTX for
  secTypes OPT and FOP. Actual level count/quality unmeasured (Stage 5 task).
- Evidence: Stage 0 report section 6, 2026-08-16.
- Date verified: 2026-08-16
- Invalidation: exchange adds/removes depth feed.

### 5. Current coupon rate and which contracts it applies to

- Claim: — (UNVERIFIED. `AccruedCash = 0.00` on an empty account proves
  nothing. Verify from account statements / accrual on a real paper or live
  position; never hardcode. Re-verify at most every 30 days.)
- Evidence: —
- Date verified: —
- Invalidation: rate is variable.

### 6. Contract universe size and full list

- Claim: **≥2,964 contracts (2,900 tradable) across 64 underlying indexes**,
  as a lower bound. Coverage observed: US macro (GDP, RGDP 234, UNR 122,
  USCE 220, CPI variants), US state GDP (CA/FL/GA/IL/NJ/NY/OH/PA/TX/WA,
  ~88–120 each), international GDP (HK 178, SG 204, JP 90, AU 54, DE 36),
  climate (GT/GTM global temperature 122 each, GTTA/GTTM, ACD 70, HCAB,
  HCAT4, RCSTH 148, RCSEA 16), misc (HLF 30, USR 8, CAREC 8).
  **Confirmed ABSENT today (secdef reports no chain): FF (Fed Funds), CPI
  (headline), CPIY, INFLM, EZR, GCE, RCNYC, DISSA** — despite Fed Funds
  being ForecastEx's flagship product in marketing. Unknown whether this is
  a temporary listing gap or an entity/residency restriction (like US
  election contracts); recheck periodically. Note `USR` = "US Recession",
  not a rates product — no rates underlying is currently visible at all.
- Evidence: Stage 0 report section 2 + `data/ibkr_universe_live.json`.
- Date verified: 2026-08-16
- Invalidation: continuous — new listings/expiries; refresh via resolver.

### 7. Historical bar data via reqHistoricalData — available? How far back?

- Claim: — (UNVERIFIED; probe to be added in Stage 5 with the pacing layer.
  Note: `ushmds` historical-data farm showed "not connected" during the
  2026-08-16 session — availability may be entitlement-gated.)
- Evidence: —
- Date verified: —
- Invalidation: IBKR data policy change.

### 8. Min/max order sizes and per-contract position limits

- Claim: — (UNVERIFIED. Observed on sample tradable contract:
  `multiplier='1'`, `minTick=0.01`; whatIf probes used quantity 1.)
- Evidence: —
- Date verified: —
- Invalidation: exchange rule change.

## Additional verified facts

- **Commission: $0.01 USD per contract, billed as a commission line.**
  Every accepted whatIf (LMT/MKT/conditional, qty 1) reported
  `commission=0.01 commissionCurrency='USD'` (min/maxCommission were the
  unset DBL_MAX sentinel). This CONTRADICTS the migration spec's claim that
  the fee is embedded in a $1.01 YES+NO price sum with "no separately billed
  commission" — the API observation wins: model it as a flat per-contract
  commission, with the price sum ≈ $1.00 at mid. Re-verify against real
  fills on paper. (2026-08-16, follow-up report section 5.)
- **Price-sum check (one illiquid sample pair, delayed quotes):**
  YES bid/ask 0.03/0.06, NO bid/ask 0.94/0.97 → bid-sum 0.97, ask-sum 1.03,
  **mid-sum 1.00** — consistent with the flat-commission model above, not
  with a 1.01 embedded sum. (2026-08-16, Stage 0 section 4.)
- **Delayed market data works without a paid subscription** (market data
  type 4 returned real bid/ask/close). Live-feed subscription cost still
  unknown → `docs/operating_costs.md`. (2026-08-16)
- **Discovery mechanics** (matter for the Stage 3 resolver):
  - Symbol-less wildcard `reqContractDetails` on FORECASTX gets NO response
    at all (not even error 200) — every such request times out.
  - Per-symbol OPT wildcard returns full chains fast for most symbols; a
    minority (FF, CPI, CPIY, INFLM, …) also get no response — resolve those
    via `reqSecDefOptParams` on the underlying IND conId instead.
  - Order presets rewrite TIF and emit error 10349, which aborts an
    ib_async whatIf round-trip — always set `tif` explicitly.
  - `reqMatchingSymbols` is the only broad entry point that works
    (~1 req/sec limit); scanner has no FORECASTX instrument codes.
  (2026-08-16, Stage 0 reports.)
- **Paper/live parity: OPEN.** The 2026-08-16 session was live (user's
  Gateway logged into the live account on port 4002 — port number does NOT
  imply mode). Run the script against a paper session with
  `--compare data/ibkr_universe_live.json` once the paper account exists.
