# Final Migration Report -- Polymarket -> IBKR ForecastEx

Stage 8 deliverable #7 (the capstone). Written 2026-08-23.

Scope: every assumption baked into the OLD Polymarket codebase that the move to
IBKR ForecastEx invalidated, with re-verified `file:line` citations and how each
was handled, plus an honest stage-by-stage completion summary. Read-only synthesis
of `docs/MIGRATION_CHECKPOINT.md` (authoritative status), `docs/venue_semantics.md`
(verified venue facts), `docs/feature_migration_audit.md` (per-feature verdicts),
`docs/gtm_model_report.md`, and the git commit trail.

All line numbers below were re-grepped against the working tree at commit
`b548283`. The executor rewiring (`ef4e31f`) shifted several numbers; where an
assumption lived in code that has since been rewritten or retired, the citation
names the commit whose blob still contains it (`git show <commit>:<path>`).

---

## 1. Executive summary

### What the migration did

The system was a quantitative prediction/trading stack aimed at Polymarket tech
markets. ForecastEx (accessed through IBKR / ib_async) is a fundamentally
different venue: event contracts are exchange-listed `OPT` contracts with
**separate conIds for YES and NO**, a flat **$0.01/contract** commission billed
as a line item, a persistent Gateway session instead of stateless HTTP, delayed
data that is free, pace-limited historical requests, and an account-level
market-data-line budget (~100 lines) far smaller than the ~2,900 tradable
contracts. The migration:

- Introduced a venue-neutral domain layer (`MarketId`, `FeeModel`, `CarryModel`,
  `Market`, `VenueAdapter`) so venue identifiers and pricing conventions stop
  leaking across modules.
- Built the IBKR adapter package (connection/heartbeat supervisor, client-id map,
  contract resolver, historical pacer, snapshot-sweep quote recorder) and an
  import ban (ruff TID251) confining venue libraries to `src/venues/`.
- Reparameterised the EV/Kelly math for a real fee and carry instead of a
  frictionless $1.00 binary.
- Retired the live Polymarket on-chain/CLOB execution path to `legacy/`, gated
  the entire Polymarket runtime off (`POLYMARKET_RUNTIME=1` required to run it),
  and rewired `TradingExecutor`/`OrderManager` onto `VenueAdapter` with a
  paper-only IBKR order path.
- Audited every feature/predictor/label/validation mechanism for the new venue
  (Stage 6), and -- per an explicit user decision -- pursued the **climate lane
  first** (GTM monthly global-temperature EMOS), since all 18 tech questions map
  to nothing tradable on ForecastEx.

### Current state (Stage 0-8)

| Stage | Title | State | What remains / who owns it |
|---|---|---|---|
| 0 | Entitlement gate | DONE (live) / BLOCKED (paper parity) | Live gate PASSED 2026-08-16 (2,964 contracts, 2,900 tradable). Paper-parity compare + legal-entity readoff -> USER (provision paper account, read Client Portal). |
| 1 | Venue abstraction | DONE | Domain layer, VenueAdapter, import ban, executor rewired (ef4e31f/b548283). Engineering. |
| 2 | Connection layer | DONE | GatewaySession + Task Scheduler recorder running. IBC headless auto-login intentionally NOT configured (USER decision; stores creds on disk). |
| 3 | Contract identity | DONE | MarketId + SQLite resolver; YES/NO separate conIds confirmed. Engineering. |
| 4 | Fees, carry, settlement | PARTIAL | EV/Kelly + executor re-pricing done. Coupon-rate verification open -> USER (read from statements/accruals; CarryModel refuses stale rates). |
| 5 | Market data & pacing | PARTIAL | Pacer, delayed snapshot recorder, and the SubscriptionManager (line budget + priority eviction + subscribe-rate limiter, `subscriptions.py`) done; the historical-bars/depth probe script (`scripts/probe_ibkr_history.py`) is written but must be RUN against a live Gateway to record findings. Wiring the streaming/depth recorder onto the manager is the remaining build. |
| 6 | Modelling consequences | DONE | Feature audit (30+ verdicts) + climate-lane decision made. Engineering + USER. |
| 7 | Paper trading | PARTIAL / BLOCKED | Mode guards + paper entrypoint done; untested against a real paper session -> USER (provision paper account). |
| 8 | Deliverables | PARTIAL | This report closes the last missing item; `operating_costs.md` stays a skeleton until the live-feed cost is knowable (needs live Gateway). See section 3. |

### What remains (condensed)

- **USER**: provision the paper account (unblocks Stage 0 parity + Stage 7); verify
  coupon rate from statements (Stage 4); read legal entity off Client Portal;
  schedule `collect_climate_labels.py`; `git push origin main`.
- **ENGINEERING**: run `scripts/probe_ibkr_history.py` when the Gateway is up (fills `docs/stage5_probe_findings.md`); wire the streaming/depth recorder onto `SubscriptionManager` (Stage 5,
  need live Gateway); rewire remains validated only against fakes -- the executor
  has never touched a real paper session; go-live is a 365-day paper record away.

Deviation note: the checkpoint states the tree was committed through `ef4e31f`;
the working tree is actually at `b548283` (this report and the Stage-5/8 finish work add further commits; named "Checkpoint: executor rewired, paper
entrypoint in place"), one commit further. Citations use the current tree.

---

## 2. Invalidated assumptions

The heart of this deliverable. One row per assumption. Every `file:line` was
re-grepped at `b548283`; rows citing a commit reference code that has since been
rewritten or retired (verify with `git show <commit>:<path>`).

| # | Assumption (as it lived in the old code) | Where it lived (re-verified) | Why it is false at IBKR ForecastEx | How it was handled |
|---|---|---|---|---|
| 1 | **NO price = 1 - YES price** is an identity | `src/math/expected_value.py:81` (`no_price = 1.0 - market_price_yes`, in `expected_value_no` def:64); `src/math/kelly.py:142` (`kelly_for_no` def:125); `src/features/market_features.py:28` (`"market_no_price": 1.0 - yes_price`); OLD `src/trading/order_manager.py:85` `price = 1 - edge.market_prob` (commit d3dac8d) | YES and NO are **separate conIds** with their own books; the sum is a market outcome (mid-sum ~1.00), not an identity. `1 - yes` manufactures a phantom NO quote. | Reparameterised via `FeeModel.no_price_from_yes` (`src/domain/fees.py:28`). Math keeps `1 - yes` only as a default when no fee model is given; `edge_to_order` now prefers the NO leg's own quote, then the FeeModel complement, then `1 - yes` as a research-only fallback (`src/trading/order_manager.py:109-114`). |
| 2 | **Fee is embedded in a $1.01 YES+NO price sum** (no separately billed commission) -- the migration spec's own claim | Migration spec text (not code); contradiction recorded at `docs/venue_semantics.md:177-188` and `src/domain/fees.py:6-8` | whatIf on every accepted order returned `commission=0.01 USD` billed separately; sampled YES+NO **mid-sum ~= 1.00**, not 1.01. API observation wins over spec text. | Modelled as `FlatCommissionFeeModel` (`src/domain/fees.py:43`) with `yes_no_price_sum()` ~= 1.00 and a per-contract `execution_cost` (`:72-73`); the spec/whatIf disagreement is documented in the fee-model docstring. |
| 3 | **$1.00 binary payout, zero fees** in EV/Kelly (frictionless) | `src/math/expected_value.py:53,56` (original EV had no fee/carry terms); `src/math/kelly.py` base formula | A real $0.01/contract commission plus a coupon-bearing settlement (carry) change break-even edge; on a $0.05 contract commission alone is ~20% of premium. | Added optional `fee_model`/`carry_model`/`holding_days` to EV and Kelly (`expected_value.py:57-60,78-91`; `kelly.py:73-97`, carry `f = p*c/(c-r) - q*c/(1+r-c)`), bit-identical when defaulted (commit 6e97be2). |
| 4 | **Market identity = conditionId / tokenId / slug strings** passed bare across modules | Old Polymarket string IDs throughout; scheme documented at `src/domain/ids.py:3-4` | ForecastEx identifies by integer `conId`, and YES/NO are two conIds; three incompatible string schemes cannot cross module boundaries safely. | Introduced the `MarketId` value object (`src/domain/ids.py:16`), venue-tagged with `con_id`/`polymarket` constructors (`:41,:45`); "neither may leak across module boundaries". |
| 5 | **The live order path worked** | OLD `src/trading/executor.py:236` `yes_token_id = getattr(result.question.market, "yes_token_id", "")` (commit d3dac8d) -- read off `MarketData` that never carried the attribute (getattr default `""`); OLD `src/trading/order_manager.py` `OrderStatus.FILLED` (defined :38) was only READ (:126,:188,:190), never assigned -- the only status writes were SIMULATED/REJECTED/CANCELLED (:150,:160,:180) | The token-id fields did not exist on `MarketData`, and no code path ever set FILLED, so a "filled" order could never be recognised: dead code masquerading as an execution path. | Rebuilt on `VenueAdapter`: `TradingExecutor`/`OrderManager` (`src/trading/executor.py:126`, `order_manager.py:163`), fill sync via `get_order` sets FILLED (`order_manager.py:273,299`); simulated vs venue paths; commits ef4e31f/b548283. |
| 6 | **Mode is implicit with a safe default** (dry-run assumed) | `scripts/trade.py:58` (`--dry-run` `default=True`), mode resolution `:190-191` | Silent-default mode is dangerous against a real broker; a mis-set default could route live. | The IBKR path makes mode explicit and mandatory: `src/venues/ibkr/settings.py:33` `port: int` and `:35` `trading_mode: Literal["paper","live"]` have **no defaults** (`:4` docstring), cross-checked against the account prefix on connect (`adapter.py:120-123`). The old `trade.py` survives but is gated off (`refuse_polymarket_runtime`, exit 2, `trade.py:177-179`). |
| 7 | **VPIN is computable and used**, and the **liquidity risk check is active** | `src/agent/researcher.py:126` `price_change_24h=0.0` hardcoded -> `estimate_vpin_from_price_movement` == 0.0, so `vpin_flag` (`:129`) is always False; `src/trading/risk_manager.py:174` liquidity check fires only when `market_liquidity > 0`, but the param defaults to 0.0 (`:90`) and OLD `executor.py:218` called `check_trade` **without** passing it (commit d3dac8d) | VPIN was inert on Polymarket already (constant 0 input); the liquidity gate was a no-op because no caller supplied a value; on IBKR there is no trade-flow feed and no USD-liquidity field at all. | Documented inert in the audit (VPIN -> RETIRE, `feature_migration_audit.md`); VPIN discounts removed from the ported edge path. The rewired executor now threads `market_liquidity=intent.liquidity_usd` (`executor.py:285`, field `:50`) so the gate is venue-conditional. Honest caveat: the back-compat path still sources a Polymarket USD-liquidity value (`executor.py:200`); an IBKR-native liquidity proxy (quoted size/spread) is not yet wired. |
| 8 | **Gas / on-chain modelling exists and must be removed** (spec assumption) | No such model in `src/` -- only a stray comment `src/config.py:32` ("funder address for gas") and a never-built placeholder `src/trading/wallet.py:74-75` ("full implementation needs web3"); live CLOB order code at `legacy/polymarket_onchain/clob_orders.py:18,37-38` | There was nothing to remove: the system never modelled gas or settled on-chain. The only real venue-write surface was a thin CLOB order call. | The live CLOB order/cancel + wallet code was retired to `legacy/polymarket_onchain/` (commit 3557f88); the one sanctioned read path is `readonly_clob_client` (`src/venues/polymarket.py:39`), and the import ban keeps `py_clob_client` inside `src/venues/`. |
| 9 | **Cross-sectional / pooled features are valid at scale** | Cluster features `features/correlation_features.py:187` (`cluster_id`) keyed on `config/market_clusters.json` (dead Polymarket numeric IDs: iran / russia_ukraine / trump_presidency), path at `:47`; `opportunity_score` volume normalizer `src/math/entropy.py:47` (`math.log1p(1_000_000)`); shuffled CV `model/train_outcome.py:374` (`StratifiedKFold(..., shuffle=True)`) | The old cross-section was a curated ~50/50 political/tech universe. The new universe is strike ladders monotone in strike that settle off a single print; every pooled constant, peer aggregate, and cluster ID is void. Shuffled StratifiedKFold **leaks**: siblings of a test strike sit in train. | Audited **suspect by default** (every `XS` row in `feature_migration_audit.md`): cluster config -> RETIRE (rebuild mechanically from symbol/expiry/strike), `opportunity_score` liquidity half -> RETRAIN, StratifiedKFold -> redesign to GroupKFold by (symbol, expiry). Flagged and not yet reimplemented (climate lane does not use them). |
| 10 | **Advanced validation machinery is present** (purged/embargoed k-fold, triple-barrier labels, sample weighting, deflated Sharpe / PBO) | Nowhere -- a repo-wide grep for purged / triple-barrier / deflated-sharpe / embargo returns **zero hits** | The spec's validation language could be misread as describing this repo; it never existed here. | Documented honestly as net-new backlog, not "ported" (`feature_migration_audit.md:397-407`). What DOES exist (temporal 60/20/20 split, causal features, pre-registered eval) is recorded and must not be weakened. |
| 11 | **Stateless HTTP with no shared pacing** is sufficient | Old collectors open a fresh client per call: `src/collectors/polymarket.py:97,121` (`async with httpx.AsyncClient(...)`), no rate limiter, no session | IBKR requires a persistent authenticated Gateway session, respects historical pacing limits, and shares a small market-data-line budget (~100) across client ids vs ~2,900 contracts. | Persistent `GatewaySession` with active heartbeat + backoff + resubscription (`src/venues/ibkr/gateway.py:110`); `HistoricalPacer` (`src/venues/ibkr/pacing.py:56`; 15s-identical `:25`, 60-per-10min `:29`, BID_ASK x2); snapshot-sweep recorder sized to `max_data_lines` (`settings.py:41` = 100; design note `recorder.py:3-11`). |

Supporting facts for the table above (unchanged verified venue semantics):
YES=`right=C`, NO=`right=P`, separate conIds (`venue_semantics.md` Q2); flat
$0.01/contract commission and mid-sum ~1.00 (`venue_semantics.md:177-188`);
delayed data (type 4) free; depth feed exists but unmeasured; coupon rate and
historical bars still UNVERIFIED.

---

## 3. Stage 8 deliverables -- present / absent

The checkpoint's Stage 8 enumerates the migration's tangible deliverables. Status
re-checked against the tree:

| # | Deliverable | Status | Location |
|---|---|---|---|
| 1 | Stage 0 entitlement script | PRESENT | `scripts/verify_ibkr_entitlements.py` |
| 2 | Stage 0 reports (+ follow-up) + universe snapshot | PRESENT | `docs/stage0_report_2026-08-16_live.md`, `..._followup.md`, `data/ibkr_universe_live.json` |
| 3 | Verified venue semantics | PRESENT | `docs/venue_semantics.md` |
| 4 | Feature migration audit (Stage 6) | PRESENT | `docs/feature_migration_audit.md` |
| 5 | Operating-costs doc | PRESENT | `docs/operating_costs.md` -- delayed data free, $0.01 commission and ~100 line budget documented; only the live-feed subscription PRICE stays UNKNOWN (needs Client Portal) |
| 6 | IBKR adapter package + running recorder | PRESENT | `src/venues/*`, recorder under Task Scheduler since 2026-08-17 |
| 7 | IBKR runbook | PRESENT | `docs/ibkr_runbook.md` |
| 8 | Climate oracle research | PRESENT | `docs/climate_oracle_research.md` (+ `docs/gtm_model_report.md`) |
| 9 | **Final migration report (this document)** | PRESENT (new) | `docs/migration_report.md` |

Every enumerated Stage 8 deliverable is now present. The only partial is the
operating-costs skeleton, which cannot be completed until a live market-data
subscription price is observable.

---

## 4. What is NOT done (be precise and honest)

This is a checkpoint, not a finished go-live. Concretely:

- **The rewired executor has never run against a real paper session.** It is
  validated only against fakes/unit tests; `scripts/run_ibkr_paper.py` refuses
  to transmit unless `IBKR_TRADING_MODE=paper` AND the account is DU/DF, and no
  such account exists yet. Blocked on **paper-account provisioning** (USER).
- **Go-live is a 365-day paper record away.** The GTM model's *hindcast* gate
  passes (expanding-window CRPS skill 0.49 at lead 1 down to 0.26 at lead 6,
  PIT near-flat), but the live paper gate requires >= 365 paper days AND >= 12
  resolved months; current progress is 0/0 (`gtm_model_report.md` section 4).
  The paper loop writes signals only -- **no orders**. The Dec-2026 ~60-point
  model-vs-market gap is explicitly flagged as untrustworthy, to be adjudicated
  by the paper record, not traded.
- **Two Stage-5 probes are pending a live Gateway**: the streaming-tick + depth
  recorder with a subscription-line manager, and the `reqHistoricalData`
  availability/depth probe. The Gateway is currently offline (weekly re-login
  due), so neither could be run for this report.
- **Coupon rate is still UNVERIFIED** (Stage 4); `CarryModel` refuses stale or
  unverified rates by design, so carry currently contributes nothing until a
  real accrual is read off statements (USER).
- **The retargeting itself is a live decision.** The 18-question corpus maps to
  nothing tradable; only the climate lane has an operational model. Macro
  (Option 1) and the two-venue split (Option 3) remain flagged, not chosen.

Nothing in the invalidated-assumptions table above should be read as "fully
re-derived": items 7, 9 in particular are *documented and gated*, with the actual
retrain deferred until the chosen lane accumulates venue data.
