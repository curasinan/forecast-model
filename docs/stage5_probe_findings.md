# Stage 5 probe findings: historical bars + market depth

Status: **PENDING** -- run `python scripts/probe_ibkr_history.py` against a
live Gateway (127.0.0.1:4002, logged in). The Gateway was offline at the time
this skeleton was written (weekly re-login due). The script overwrites this
file with dated results.

Answers two open questions from `docs/venue_semantics.md`:

- **Q4 -- native order book with depth, or top of book only?** A depth feed
  exists per `reqMktDepthExchanges` (FORECASTX OPT/FOP), but the actual level
  count/quality is unmeasured. The probe subscribes `reqMktDepth(numRows=5)`
  briefly and reports how many levels come back.
- **Q7 -- historical bar data via `reqHistoricalData`, and how far back?**
  The probe runs `reqHeadTimeStamp` and `reqHistoricalData` for TRADES,
  MIDPOINT, and BID_ASK on GTM/UNR/RGDP YES legs. Note: during the 2026-08-16
  Stage 0 session the `ushmds` historical-data farm showed "not connected",
  so availability may be entitlement-gated -- the probe will show whether the
  requests return bars, "no data", or a permission error (codes 162/165/200).

## Results

(paste the probe output here, dated, once it has run against a live Gateway)
