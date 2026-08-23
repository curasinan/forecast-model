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
Still open: legal entity confirmation, coupon rate, max order size /
position limits, paper parity. (Historical bars and depth answered 2026-08-23.)

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

- Claim: **effectively top-of-book only.** `reqMktDepthExchanges` lists
  FORECASTX (OPT/FOP) and `reqMktDepth(numRows=5)` answers, but returns
  exactly ONE level per side on quoted contracts (e.g. GTM bid 0.86 x 10,
  UNR bid 0.92 x 10) and zero levels on unquoted ones. Size at the top is
  visible; there is no resting ladder beyond L1 to record. This validates
  the snapshot-sweep recorder over a streaming depth recorder.
- Evidence: `scripts/probe_ibkr_history.py` -> `docs/stage5_probe_findings.md`,
  2026-08-23 (GTM 830396294, UNR 753739783, RGDP 712857395).
- Date verified: 2026-08-23
- Invalidation: exchange introduces multi-level depth; re-run the probe.

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

- Claim: **daily (EOD) bars: NOT available** — every 1-day request for
  TRADES/MIDPOINT/BID_ASK returns error 162 "No data of type EODChart is
  available for the exchange 'FORECASTX' and the security type 'Option'".
  **Intraday bars: AVAILABLE** — BID_ASK at 1-hour bars over '2 D' returned
  the full 48 bars on quoted contracts (GTM, UNR); unquoted far-dated
  contracts (RGDP 2031) return "HMDS query returned no data".
  `reqHeadTimeStamp`: MIDPOINT/BID_ASK history begins **2026-01-17**
  (~7 months before the probe); TRADES head = first trade (GTM 2026-05-07,
  UNR 2026-04-22; none for RGDP).
  Consequence: hourly quote history since 2026-01-17 can be BACKFILLED
  through the HistoricalPacer (BID_ASK counts double: ~30 requests/10 min),
  which recovers recorder gaps (e.g. the 2026-08-17..23 outage). Full-
  universe backfill is ~20k requests (~5 days of pacing); a prioritized
  subset (climate-lane contracts) is a few hours -- scope as a follow-up.
- Evidence: `docs/stage5_probe_findings.md`, 2026-08-23.
- Date verified: 2026-08-23
- Invalidation: IBKR HMDS policy change; re-run the probe.

### 8. Min/max order sizes and per-contract position limits

- Claim: **minimum order size = 1 contract, size increment = 1** (verified
  on GTM/GT/ACD/USCE/HCAB/HLF/HCAT4 OPT details: `minSize=1.0`,
  `sizeIncrement=1.0`). Max size / position limits still unverified.
- Evidence: reqContractDetails full-field dump, 2026-08-18.
- Date verified: 2026-08-18 (min/increment only)
- Invalidation: exchange rule change.

### 8b. Settlement timing per climate contract (API-side evidence)

`realExpirationDate` vs `contractMonth` encodes each product's publication
lag (all with `lastTradeTime` in US/Central; trading hours ~23h/day):

| Symbol | Contract month | Real expiration | Implied lag |
|---|---|---|---|
| GTM Dec-2026 | 202612 | 2027-02-12 10:00 | ~6 weeks (monthly temp publication) |
| GT 2026 (annual) | 202612 | 2027-01-08 10:00 | ~1 week |
| ACD 2026 (annual CO2) | 202612 | 2027-01-05 08:00 | ~days (Mauna Loa cadence) |
| USCE 2026 (US emissions) | 202612 | 2027-04-30 16:00 | ~4 months (accounting lag) |
| HCAB/HLF 2026 (hurricanes) | 202612 | 2026-12-31 12:59 | season end + 1 month; too soon for HURDAT reanalysis -> operational counts |
| HCAT4 Sep-2026 | 202609 | 2026-09-30 23:59 | in-season monthly window |

- Evidence: reqContractDetails dump 2026-08-18 (scratchpad probe; conIds
  830396294, 712857050, 712856754, 732957203, 871016074, 871016195,
  902537007).
- Date verified: 2026-08-18
- Invalidation: per-listing; re-read from contract details, never assume.

## Climate contract resolution oracles (verified 2026-08-18)

Sources: the venue's own Terms & Conditions PDFs at
`data.forecastex.com/regulatory/` plus the ForecastEx LLC Rulebook
(Feb 17, 2026) and CFTC filings. Full evidence trail with verbatim quotes
and URLs: `docs/climate_oracle_research.md`. Cross-checked against the
API-side settlement lags in section 8b.

| Family | Settling series (exact) | Baseline / units | Strike rule | Vintage | Early res.? |
|---|---|---|---|---|---|
| GT (annual temp) | NOAA NCEI annual Global Climate Report, land+ocean anomaly | 1901-2000 avg, degC (Celsius governs) | strictly > | first release (no reopen after settle) | No |
| GTM (monthly temp) | NOAA NCEI Climate at a Glance, global land+ocean monthly | 1901-2000 avg, degC | strictly > | first-published monthly value | No |
| GTTA/GTTM (thresholds) | same series, re-based to pre-industrial 1850-1900 via venue offset **currently +0.16 degC** (may change with NOAA methodology) | pre-industrial, degC | strictly >, cumulative ("any year/month by [year]") | current revised table at each check | **Yes** (Rule 413(b)) |
| ACD (annual CO2) | NOAA GML Mauna Loa ANNUAL MEAN (`co2_annmean_mlo.txt`) | absolute ppm | strictly > | first release (Jan 5, 8:00 CT) | No |
| MACD (monthly CO2, unlisted) | NOAA GML Mauna Loa monthly (5th of month, 8:00 CT) | absolute ppm | strictly > | first release | No |
| HCAB (hurricane count) | NHC Tropical Cyclone Archive public advisories -- **NOT HURDAT2 reanalysis** | integer count, Cat1+ (>=64kt) forming in the **calendar year** (not the Jun-Nov season) | **inclusive** "at least X" (>=) | advisories as issued; post-settlement reanalysis irrelevant | **Yes** |
| HLF (landfall) | NHC advisories; landfall = eye intersects land, interpolated between advisories if needed | **strikes are REGIONS, not counts** (FL/TX/NC/LA, US counties, Bahamas/Cuba/Mexico/DR/Honduras); Cat3+ AT time of landfall; within region **or 50 straight-line miles of its border** | binary per region | as issued | Yes |
| HCAT4 | NHC advisories; Cat 4+ in the US | binary | as issued | as issued | Yes |

Cross-cutting rulebook facts:
- **Rule 603(e): "Settlement is irrevocable and unconditional"** -- the
  value as published at Resolution Time governs; later source revisions
  cannot reopen a settled contract. Pre-settlement data errors go to Event
  Review (Rule 415), whose determinations are final.
- Rule 413(a): the venue may replace a degraded Source Agency.
- Resolution delayed if the source delays publication (all families).
- Settlement no later than the day after Expiration Time.
- **Strike-semantics trap: temperature/carbon are strictly-greater-than;
  hurricane counts are at-least (inclusive).** Model code must not assume
  one convention.
- GTTM T&C contains a drafting inconsistency (its Underlying paragraph
  says "any year", its Sample Question and Resolution Time say monthly) --
  venue drafting bug, recorded; monthly reading is the coherent one.
- Sibling products found with published T&Cs but no current chains: TROP
  (tropical storms >=34kt), MHC/MHCMA (monthly major hurricanes), HLFRE
  (regional landfall), LHL (live hurricane location by peak gust).
- IBKR serves a per-conid rules endpoint
  (`tws.proxy/public/forecasttrader/contract/rules`) whose
  `market_rules_link` points at the governing T&C PDF -- a programmatic
  oracle-discovery hook for the resolver (Stage 5+ item).
- Modeling note: `Market.strike` is a genuine numeric threshold for
  temperature/carbon/count families but an opaque REGION CODE for HLF --
  do not feed HLF strikes into numeric ladder features.

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
