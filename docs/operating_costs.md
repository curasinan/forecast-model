# IBKR ForecastEx operating costs

Same rules as `docs/venue_semantics.md`: every entry states the **claim**,
the **evidence**, the **date verified**, and the **invalidation condition**.
UNKNOWN / UNVERIFIED entries stay in the file so the open questions remain
visible; nothing here is admissible from marketing or spec text alone.

## Market data

### Delayed data (reqMarketDataType 4)

- Claim: free -- no paid subscription required. This is what the quote
  recorder (`src/venues/ibkr/recorder.py`) currently captures. Delayed
  data lags roughly 15-20 minutes: fine for daily-scale features, NOT
  usable for microstructure work (spread/fill dynamics at event time,
  queue behavior, short-horizon adverse selection).
- Evidence: Stage 0 price-sum check returned delayed bid/ask/close with no
  subscription (`docs/stage0_report_2026-08-16_live.md` section 4);
  recorder verification sweep against the live session, 2026-08-16.
- Date verified: 2026-08-16
- Invalidation: IBKR changes the delayed-data policy for FORECASTX.

### Live ForecastEx feed

- Claim: subscription cost UNKNOWN. To be read off Client Portal ->
  Settings -> Market Data Subscriptions by the account holder and recorded
  here with the date.
- Evidence: --
- Date verified: --
- Invalidation: n/a until verified; then any IBKR pricing change.

### Market data lines

- Claim: ~100 simultaneous market data lines by default; the allocation is
  account-level and SHARED across all client ids (recorder, research, and
  any future trading process draw from the same pool). Doc-level default
  only -- NOT yet observed empirically (no line-limit rejection seen).
  This limit is why recorder v1 does transient snapshot sweeps in 50-conId
  batches over the ~2,900-contract universe instead of holding streaming
  subscriptions; streaming a prioritized subset is the Stage 5 follow-up.
- Evidence: IBKR documentation default; not yet measured on this account.
- Date verified: -- (treat the number as approximate until a limit error
  or Client Portal page confirms it)
- Invalidation: quote booster packs, equity-based line increases, or IBKR
  policy change.

## Commissions and account minimums

### Commission

- Claim: flat $0.01 USD per contract, billed separately as a commission
  line (NOT embedded in a $1.01 YES+NO price sum). Cross-reference:
  `docs/venue_semantics.md`, "Additional verified facts".
- Evidence: whatIf probes (LMT/MKT/conditional, qty 1) all reported
  `commission=0.01 commissionCurrency='USD'`, 2026-08-16 follow-up report
  section 5.
- Date verified: 2026-08-16
- Invalidation: fee schedule change; re-verify against real paper/live
  fills.

### Monthly minimums / inactivity fees

- Claim: UNKNOWN whether any monthly minimum, inactivity fee, or market
  data minimum applies to this account tier and servicing entity (entity
  itself still unconfirmed -- venue_semantics question 1).
- Evidence: --
- Date verified: --
- Invalidation: n/a until verified.

## Cost offsets

### ForecastEx incentive coupon

- Claim: UNVERIFIED as a cost offset. The advertised ~3.14% APY daily
  accrual on position value would offset carry, but the actual rate,
  accrual base, and eligibility are unmeasured (`docs/venue_semantics.md`
  question 5). `AccruedCash = 0.00` on an empty account proves nothing.
- Evidence: --
- Date verified: --
- Invalidation: rate is variable; once positions exist, re-verify from
  statements or observed accrual at most every 30 days
  (`ForecastExCouponCarry` enforces this staleness bound).
