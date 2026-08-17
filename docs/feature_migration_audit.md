# Feature Migration Audit (Stage 6) -- Polymarket -> IBKR ForecastEx

Date: 2026-08-18. Status: Stage 6 deliverable of the IBKR migration.

## What this audits, and from what

Every feature, predictor, selector, label, decision rule, analysis study, and
validation mechanism in the repo, each with a migration verdict for the IBKR
ForecastEx venue. Merged from three independent audits (Stack A:
question/LLM/evidence stack; Stack B: order-book/LightGBM stack + analysis
studies; C: weather track vs climate contracts + question set vs universe),
run 2026-08-16 against:

- `docs/venue_semantics.md` -- empirically verified venue facts only (Stage 0
  live-session probes, 2026-08-16). Key facts used throughout: YES and NO are
  SEPARATE contracts (right=C/P per conId); flat $0.01/contract commission
  (contradicts the spec's embedded-fee claim; API observation wins); mid-sum
  YES+NO ~= 1.00; minTick 0.01; delayed data works unsubscribed; GTC resting
  limit orders accepted; depth feed exists but is unmeasured; coupon rate and
  historical bars UNVERIFIED.
- `data/ibkr_universe_live.json` -- 2,964 rows (64 IND underlyings + 2,900
  OPT), snapshot 2026-08-16T12:40Z.
- Recorder: 20-min delayed parquet sweeps, columns `ts_utc, con_id, symbol,
  expiry, strike, right, bid, ask, last, close, bid_size, ask_size,
  data_type`. Top-of-book only, no trade volume, no resolutions.
- Table style follows `docs/sports-migration-scoping.md` section 6.

Verdict vocabulary: **KEEP** (works as-is), **PORT** (mechanical remap, same
logic), **RETRAIN** (machinery survives, every fitted number/weight/threshold
must be re-derived on new-venue data), **RETIRE** (no analogue or premise
void), **USER-DECISION** (research/strategy call -- flagged in the final
section, never made here).

**Cross-sectional rule (spec requirement):** any row marked `XS` in the
tables below depends on the *distribution across markets* (ranking,
pooling, normalization constants fitted to the old universe, peer
aggregates, shuffled CV over outcomes). Every XS item is **suspect by
default**: the old cross-section was ~curated 50/50 Polymarket political/tech
markets; the new cross-section is strike ladders whose members are monotone
in strike and settle off a single print. No XS number transfers; each is
either retired or re-derived. Consolidated XS inventories appear after each
stack's tables.

## Structural advantage: scheduled resolution time

The `Market` model already carries the field (`src/domain/models.py:39`,
`scheduled_resolution_time`), with the design note that feature pipelines
should prefer it over `expiry` when present. On Polymarket, resolution
timing was *estimated*; on ForecastEx it is *scheduled* -- macro contracts
settle off calendar-published releases (BLS/BEA/FOMC) and expiries are fixed.

Current state: the IBKR adapter populates it from contract expiry
(`src/venues/ibkr/adapter.py:314`, `_parse_expiry(row["expiry"])`).
**Open item:** populating it from the underlying macro release calendars
(finer-grained than expiry: exact release timestamps) is not wired -- it is
a Stage 6 data-source decision (see USER DECISIONS). This field is what
makes three verdicts below possible: the release-calendar-aware label
horizon (Stack B, labels), retiring the "low activity near deadline -> NO"
heuristic (Stack A, Bayesian), and B's note that `time_to_resolution`
becomes *more* reliable on this venue, not less.

---

## Stack A -- question/LLM/evidence stack

### A1. src/features/ -- feature extractors

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| avg_sentiment, sentiment_std, positive/negative/neutral_ratio, max_positive, max_negative | src/features/sentiment.py:41-97 | no | PORT | VADER machinery venue-free; BUT "positive sentiment -> YES" polarity is question-coupled -- on threshold contracts (UNR above X, hurricanes, recession) bad news favors YES. Needs per-underlying sign mapping (USER-DECISION downstream) |
| sentiment_momentum | src/features/sentiment.py:72-86 | no | PORT | Same polarity caveat; 7d recent-vs-older split generic |
| SOURCE_CREDIBILITY table | src/features/credibility.py:16-44 | no | RETIRE (contents) / KEEP (machinery) | Table is tech press + a literal "Polymarket": 0.80 row (line 41). Rebuild for BLS/BEA/Fed/NOAA/NHC/ECMWF; drop the Polymarket row |
| avg_credibility, high_credibility_ratio, credibility_weighted_sentiment | src/features/credibility.py:67-96 | no | KEEP (mechanism), RETRAIN (values) | Weighted-average logic venue-free; scores are hand-tuned tech priors, never calibrated |
| total_mentions, mentions_last_7d/30d | src/features/frequency.py:12-16 | no | KEEP | Pure counts over the bundle |
| mention_acceleration | src/features/frequency.py:17-25,34 | no | USER-DECISION | Mechanism ports; the "acceleration -> YES" semantic is a tech-launch assumption -- GDP/CPI/hurricane coverage accelerates on schedule regardless of outcome |
| source_diversity | src/features/frequency.py:28,35 | no | KEEP | Generic |
| days_to_deadline, deadline_urgency | src/features/temporal.py:24-25,41-42 | no | PORT | Map lastTradeDateOrContractMonth -> deadline (prefer scheduled_resolution_time). urgency clamps to 0 beyond 1 year -- long-dated annual expiries all read zero urgency (degenerate, not wrong) |
| evidence_recency_ratio, avg_evidence_age_days | src/features/temporal.py:27-38 | no | KEEP | Generic |
| market_yes_price, market_no_price, market_log_odds, market_entropy | src/features/market_features.py:24-30 | no | PORT | Map to mid of YES conId. Caveat: NO is a separate conId with its own book; 1 - yes_price is an approximation (verified mid-sum ~= 1.00, acceptable) but real NO quotes exist in the recorder and should be preferred |
| market_volume_24h, market_total_volume, market_liquidity | src/features/market_features.py:31-33 | no | RETIRE | Gamma USD fields; recorder has no volume fields at all. Nearest replacement (bid/ask size + spread) is a different quantity. See conflict note under data gaps |
| volume_intensity | src/features/market_features.py:35-37 | no | RETIRE | Both numerator and denominator unavailable on new venue |
| FeaturePipeline merger | src/features/pipeline.py:7-25, base.py:8 | no | KEEP | Pure plumbing |

### A2. src/predictors/

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| Bayesian prior = market price | src/predictors/bayesian_predictor.py:91-96 | no | PORT | ForecastEx mid works; delayed 20-min quotes = staler prior, acceptable for slow macro underlyings |
| Likelihood: mention-volume rules | bayesian_predictor.py:44-52 | no | RETRAIN | "High mentions -> YES", "no evidence -> NO" is tech-launch logic; scheduled prints resolve regardless of press volume; zero news != NO for threshold contracts |
| Likelihood: acceleration rules | bayesian_predictor.py:54-61 | no | RETRAIN | Same news-flow-implies-YES assumption |
| Likelihood: deadline-activity rules | bayesian_predictor.py:64-71 | no | RETIRE | "Low activity near deadline -> NO (0.25/0.65)" is flatly wrong for data-release resolution |
| Likelihood: source diversity | bayesian_predictor.py:74-76 | no | USER-DECISION | Diversity -> YES plausible for events, dubious for thresholds |
| Likelihood: volume_intensity rule | bayesian_predictor.py:79-81 | no | RETIRE | Input feature dead on new venue |
| Bayesian confidence item_count/20 | bayesian_predictor.py:115-118 | XS | RETRAIN | Constant tuned to tech news-flow volume; renormalize |
| FEATURE_WEIGHTS | src/predictors/classical_ml.py:37-58 | no | RETRAIN | Never trained (heuristic mode); volume_intensity weight references dead field; sentiment weights carry wrong sign for bad-news-favors-YES contracts; relearn per underlying family |
| SCALE_FEATURES ranges | classical_ml.py:61-73 | XS (implicit) | RETRAIN | Fixed normalization constants fitted to the old tech-question population |
| Market-anchor adjustment | classical_ml.py:100-149 | no | PORT | Anchoring on mid ports cleanly once weights retrained |
| ML confidence (coverage, item_count/15) | classical_ml.py:183-187 | XS | RETRAIN | Constants tuned to tech news flow |
| SYSTEM_PROMPT | src/predictors/llm_predictor.py:27-53 | no | PORT | Venue-agnostic machinery; question-coupled text ("tech industry events", AI-lab timelines) -- trivial rewrite to macro/climate framing (NBER base rates, hurricane climatology, GDP surprise distributions) |
| Prompt market-price framing | llm_predictor.py:110-128 | no | PORT | ForecastEx prices are also 0-1 dollars settling at 1.00; feed mid |
| Probability clamp [0.05, 0.95] | llm_predictor.py:154-157 | no | USER-DECISION | Strike ladders legitimately contain true prob < 0.05 contracts; the clamp (and "avoid extremes" prompt rule) systematically distorts deep-ITM/OTM strikes -- decide whether the strategy trades those strikes at all |
| JSON parse/fallback machinery | llm_predictor.py:130-162, 164-231 | no | KEEP | Venue-free |
| DEFAULT_WEIGHTS + 3 weighting strategies | src/predictors/ensemble.py:27-31, 58-150 | no | KEEP | Pure combination logic |
| compute_ensemble_weights (inverse-Brier) | ensemble.py:162-218 | XS (cross-question) | RETRAIN | Pooled Brier across Polymarket tech resolutions does not transfer; reset to DEFAULT_WEIGHTS, rebuild history from ForecastEx resolutions (macro prints resolve frequently -- accrues faster) |

### A3. src/edge/ and src/math/entropy.py

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| find_edge (edge = model - market) | src/edge/edge_finder.py:14-64 | no | PORT | Core math ports (mid -> market_price) BUT must add venue costs: half-spread on the traded side (YES ask or NO ask on its own conId) + flat $0.01/contract commission -- else edge overstated exactly like the Polymarket 0.005-buffer finding |
| Half-Kelly + NO-side Kelly | edge_finder.py:41-47 | no | KEEP | Pure math |
| VPIN Kelly discount x0.5 | edge_finder.py:49-50 | no | RETIRE | Downstream of dead VPIN |
| min_edge = 0.05 default | edge_finder.py:19 | no | USER-DECISION | Right threshold depends on ForecastEx spread + commission |
| estimate_vpin_from_price_movement | src/edge/vpin.py:34-75 | no | RETIRE | Known-inert: researcher.py:126 hardcodes price_change_24h=0.0 so VPIN == 0.0 always; log1p(100_000) normalizer is a Polymarket volume constant; no trade-flow data on IBKR recorder -- delete, do not port |
| assess_vpin thresholds/multipliers | vpin.py:29-31, 78-115 | no | RETIRE | Dies with its input |
| TECH_TAGS | src/edge/market_selector.py:24-29 | no | RETIRE | Gamma tag_slug vocabulary; ForecastEx discovery = enumerate universe JSON by symbol/underlying |
| MarketCandidate dataclass | market_selector.py:32-56 | no | RETIRE (rebuild) | New candidate needs conId, symbol, strike, right, expiry, bid/ask/sizes; half the old fields have no source |
| scan_markets (fetch+filter+rank) | market_selector.py:59-134 | XS | RETIRE (rebuild) | THE cross-sectional selector: tag search + USD min_volume/min_liquidity thresholds + universe-wide ranking. Replacement scans the ~2,900-contract universe JSON + latest recorder sweep, ranking by entropy x a size/spread liquidity proxy |
| filter_actionable price band 0.10-0.90 + min-days | market_selector.py:137-162 | no | PORT | Venue-free; on strike ladders it usefully picks near-the-money strikes -- interacts with the LLM clamp decision |
| binary_entropy | src/math/entropy.py:17-28 | no | KEEP | Pure math |
| opportunity_score (entropy x volume factor) | entropy.py:31-50 | XS (implicit) | RETRAIN | log1p(1_000_000) normalizer fitted to the Polymarket 24h-volume distribution; entropy half survives, liquidity half must be redesigned around quoted size / spread tightness -- no volume column exists |
| rank_markets | entropy.py:53-75 | XS | RETRAIN | Cross-sectional sort on the above; trivially remappable AFTER opportunity_score is redesigned, meaningless before |

### A4. src/collectors/ -- does "just retarget the keywords" hold?

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| BaseCollector, CollectorRegistry (dedupe, merge) | src/collectors/base.py:9-28, registry.py:12-52 | no | KEEP | Pure machinery keyed off Question.search_keywords |
| RSS SEARCH_FEEDS (Google News, HN search) | src/collectors/rss_feed.py:20-23 | no | KEEP | Retarget claim HOLDS for Google News keyword search ("GDP", "hurricane landfall"). HN search returns near-nothing for macro/climate -- harmless dead weight |
| RSS STATIC_FEEDS (TechCrunch/Verge/Ars) | rss_feed.py:26-30 | no | RETIRE (contents) | Mechanism holds; feed list must become BLS/BEA/Fed/NOAA/NHC/EIA. Client-side keyword filter (lines 84-100) ports unchanged |
| NewsAPI keyword search | src/collectors/news_api.py:75-83 | no | KEEP | Claim holds PARTIALLY: machinery survives; free-tier /top-headlines only surfaces headline-grade events -- CPI/jobs yes, state-GDP or CO2 readings essentially never |
| NewsAPI category=technology hardcode | news_api.py:85-94 (line 89) | no | PORT | One-line change to business/science per underlying family |
| GitHub REPO_MAP + activity premise | src/collectors/github_activity.py:29-43, 77-100 | no | RETIRE | Claim FAILS: not a keyword swap -- the evidentiary premise (repo activity as leading indicator) has no macro/climate analogue. Refill the slot with data-release collectors (BLS/BEA/FRED, NOAA/NHC, Mauna Loa CO2) on the same BaseCollector interface |
| PolymarketCollector (search, get_market_by_id, market-as-evidence, _normalize_markets) | src/collectors/polymarket.py:30-200 | no | RETIRE (replace) | Entire file is the venue. Replace with IBKR quote source reading universe JSON + recorder parquet. The quote-as-evidence CONCEPT ports: emit an EvidenceItem carrying bid/ask/mid per conId |

### A5. src/agent/researcher.py -- flow coupling

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| Collector registration set | researcher.py:95-99 | no | PORT | Swap venue collector; registry pattern unchanged |
| Live market refresh | researcher.py:105-111 | no | RETIRE (replace) | Replace get_market_by_id(condition_id) with conId lookup against latest recorder sweep; freshness <= ~20 min + delay must be surfaced to the risk layer |
| VPIN step | researcher.py:123-129 | no | RETIRE | Confirmed inert (hardcoded price_change_24h=0.0, vpin_flag always False); remove the step and the vpin_* ResearchResult fields |
| Market price default 0.5 fallback | researcher.py:156 | no | RETIRE (the fallback) | On ForecastEx every contract has a real quote; a silent 0.5 fallback manufactures phantom edge against contracts whose real mid is 0.9 -- make missing-quote a hard skip |
| Snapshot storage / tracker plumbing | researcher.py:179-213 | no | KEEP | Venue-free; feeds the (reset) calibration history |

### Stack A cross-sectional inventory (all suspect)

1. opportunity_score volume normalizer -- entropy.py:47 -- RETRAIN.
2. rank_markets -- entropy.py:53-75 -- RETRAIN.
3. scan_markets filters + ranking -- market_selector.py:59-134 -- RETIRE/rebuild.
4. VPIN volume factor -- vpin.py:69 -- RETIRE (inert anyway).
5. compute_ensemble_weights pooled cross-question Brier -- ensemble.py:162-218 -- RETRAIN (reset history).
6. SCALE_FEATURES fixed ranges -- classical_ml.py:61-73 -- RETRAIN.
7. Confidence normalizers /20, /15 -- bayesian_predictor.py:116, classical_ml.py:186 -- RETRAIN.

Everything in src/features/ proper is per-market/per-bundle -- no extractor
reads other markets.

Stack A synthesis (auditor's own): venue coupling concentrates in
PolymarketCollector, market_selector, the three volume features, VPIN, and
the volume normalizers; everything else needing work is *question-set*
coupled. The single biggest research risk is **sign polarity**, not
plumbing (three components hardcode "positive/more news -> YES"). The
strike-ladder structure breaks two soft assumptions: the LLM extremes clamp
and entropy-first selection, both of which presuppose a curated ~50/50
universe.

---

## Stack B -- order-book / LightGBM stack

### B1. features/market_features.py (FEATURE_KEYS)

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| price_now (midpoint) | features/market_features.py:274 | no | PORT | (bid+ask)/2 of YES conId; mid-sum ~= 1.00 makes YES-mid canonical; delayed quotes shift timing only |
| price_1h/6h/24h/72h_ago | market_features.py:281-285 | no | PORT | Same causal "latest <= cutoff" lookup on 20-min sweeps; staleness 15->20 min immaterial at >=6h lookbacks |
| momentum_1h | market_features.py:288-292 | no | RETRAIN | At 20-min delayed sampling a 1h delta is 3 sweeps of a wide illiquid book -- mostly quote flicker; signal content must be re-established |
| momentum_24h / momentum_72h | market_features.py:288-292 | no | RETRAIN | Computable; macro contracts move in steps at releases, not drift -- new dynamics to learn |
| volume_24h | market_features.py:278 | no | RETIRE (until recorder captures it) | Not computable: no volume field, no trade prints; IBKR delayed volume tick would need a new recorder column. (Conflict note: Auditor A calls this flat RETIRE with no equivalent; Auditor B leaves a future path open via recorder extension. Same verdict today; disposition differs only if the recorder grows a volume column) |
| spread_now | market_features.py:275 | no | PORT | ask - bid directly from sweep |
| spread_avg_24h | market_features.py:294-299 | no | PORT | ~72 obs/day at 20 min vs ~96 at 15; same computation |
| spread_widening | market_features.py:301-302 | no | RETRAIN | Computable; interpretation differs where spreads are structurally wide (sample pair: 0.03) and widen on release calendars |
| bid_depth_now / ask_depth_now | market_features.py:276-277 | no | PORT (degraded) / RETRAIN | Recorder has only top-of-book bid_size/ask_size -- a thinner quantity; full depth needs the unmeasured reqMktDepth feed (venue doc Q4, Stage 5) |
| depth_imbalance | market_features.py:304-308 | no | RETRAIN | Computable from top-of-book sizes but semantics change (single-level != book imbalance); predictive value unknown |
| time_to_resolution | market_features.py:261-264 | no | PORT | Maps to expiry/lastTradeDateOrContractMonth -- fixed, known expiries make this feature MORE reliable on ForecastEx (see scheduled_resolution_time section) |
| distance_from_round (ROUND_LEVELS) | market_features.py:45, 310-314 | no (fixed levels, not a distribution) | USER-DECISION | Whether retail round-number psychology exists on an institutional macro venue is research, not engineering; natural analog is distance-to-adjacent-strike |
| snapshot_count | market_features.py:259 | no | PORT (weak) | Keep only as a data-quality gate, not a feature |

### B2. features/correlation_features.py + config/market_clusters.json

The whole module is the cross-sectional core of Stack B -- every aggregate is
computed across the distribution of cluster peers. **All 7 rows XS, all
suspect.**

| Feature | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| cluster_id | correlation_features.py:187 | XS | RETIRE config, PORT concept | Every configured market_id in market_clusters.json:1-34 (iran, russia_ukraine, trump_presidency) is a dead Polymarket ID. ForecastEx has STRONGER natural clusters buildable mechanically from symbol/expiry/strike (same-underlying ladder; same underlying across expiries; national GDP <-> 10 state GDPs; GT<->GTM<->GTTA) -- no hand curation |
| cluster_size | correlation_features.py:188 | XS | PORT | Trivially recomputable once clusters = (symbol) or (symbol, expiry) groups |
| cluster_avg_price | correlation_features.py:201 | XS | USER-DECISION + RETRAIN | Across a strike ladder prices are monotone in strike -- a peer mean mixes thresholds and is not the same object; candidate replacement: implied-CDF summary (e.g. ladder median strike) |
| cluster_price_dispersion | correlation_features.py:203-208 | XS | USER-DECISION + RETRAIN | On a ladder, dispersion is mechanical (CDF steepness = market-implied uncertainty of the underlying) -- arguably a BETTER feature, but a different meaning; needs redesign + new data |
| cluster_avg_momentum_1h | correlation_features.py:210-212 | XS | RETRAIN | Ladder co-movement is near-mechanical (all strikes reprice on one print); signal collapses toward redundancy with own momentum -- re-measure |
| own_vs_cluster_momentum | correlation_features.py:213-215 | XS | RETRAIN | Own-strike-vs-rest-of-ladder is a genuinely interesting relative-value signal here, but has no training data yet |
| cluster_lead_signal | correlation_features.py:216 | XS | RETRAIN | Lead-lag hypothesis untested where all peers settle off the same official release |

### B3. features/labels.py

| Item | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| generate_outcome_labels / _normalize_resolution | features/labels.py:58-105, 303-323 | no | PORT (needs new capture job) | Logic is a trivial mapping BUT the recorder captures no resolutions -- a settlement/expiry capture job (YES leg pays 1.00 + coupon) must exist before any outcome label does |
| generate_price_movement_labels (+/-30-min half-window) | labels.py:113-211, 47 | no | PORT (widen window) | At 20-min cadence the window tolerates only 1 missed sweep; widen to +/-40-60 min or label yield drops on recorder gaps |
| DIRECTION_TOLERANCE = 0.005 | labels.py:48 | no | USER-DECISION | ForecastEx minTick = 0.01 -> mid moves in 0.005 quanta; a half-tick move exactly equals the tolerance and maps to direction=0. Re-choose the dead zone relative to the venue tick |
| build_training_dataset null-drop (>50%) | labels.py:49, 277-284 | per-row only | PORT | With volume_24h retired the null-fraction denominator changes; re-check the 0.5 threshold |
| 6h price-change target itself | labels.py:46; model/train.py:494 | no | RETRAIN (likely redesign) | Computable, but: (a) delayed quotes shift both endpoints (tolerable); (b) delta-mid of a 3-cent-wide illiquid top-of-book is dominated by quote noise; (c) macro contracts reprice episodically at scheduled releases -- a fixed 6h horizon straddles mostly-dead time. Release-calendar-aware horizon is the natural replacement (research call feeding a retrain; enabled by scheduled_resolution_time) |

### B4. model/train.py

| Item | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| POLITICAL_REGEX universe selector | model/train.py:112-140, 173-195 | XS (selects the training cross-section) | RETIRE | Replace with universe selection from data/ibkr_universe_live.json (symbol/underlying filters); no text regex needed on a 64-underlying venue |
| STABLE_FEATURES (momentum_24h/72h, price_now, time_to_resolution) | train.py:101-106 | XS (selection artifact) | RETRAIN | All four computable from the recorder, but "stable" is an empirical property of the old venue's data (measured 2026-05-23 on political markets); re-derive, do not carry |
| temporal_split (row-count 60/20/20) | train.py:308-335 | no (pools markets, split is temporal) | KEEP | Venue-agnostic methodology; robust to sparse ForecastEx history |
| generate_timestamps (6h grid, 72h/24h buffers) | train.py:198-226 | no | PORT | Same grid over parquet date range |
| MIN_SNAPSHOTS = 50 | train.py:84 | no | PORT (renumber) | 50 x 15 min ~= 12.5h vs 50 x 20 min ~= 16.7h; pick threshold by intended time span, not count (x0.75 rule for count thresholds) |
| trade threshold +/-0.02 | train.py:91-92 | no | USER-DECISION | 0.02 = only 2 minTicks and less than one observed spread; reset relative to spread + commission |
| confidence gate: pred_std < median of trade candidates | train.py:749-768 | XS (median over candidate cross-section at eval time) | KEEP (flag) | Mechanism ports; silently depends on a wide-enough candidate cross-section -- with few ForecastEx signals the median is noisy |
| simulate_profit (label-vs-threshold P&L) | train.py:343-400 | XS (pools trades cross-market) | RETRAIN | Ignores $0.01/contract commission and wide spreads; on a 0.05 contract the commission alone is 20% of premium -- re-simulate with venue frictions |
| LGBM_PARAMS / learning-curve sweep / feature stability / 5-seed ensemble | train.py:68-79, 587-722 | no | KEEP | Overfitting machinery is venue-agnostic |

### B5. model/train_outcome.py

| Item | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| FEATURE_COLS (price_now + momenta + all 7 cluster feats) | model/train_outcome.py:65-77 | XS (cluster block) | RETRAIN | Inherits per-feature verdicts in B1-B2 |
| CLUSTER_ID_MAPPING {iran, russia_ukraine, trump_presidency} | train_outcome.py:81-86 | XS | RETIRE | Rewrite as symbol/underlying encoding |
| StratifiedKFold (shuffle) CV | train_outcome.py:374 | XS (assumes cross-sectionally independent outcomes) | RETRAIN (redesign CV) | **The single most dangerous silent transfer.** Every strike of one (underlying, expiry) resolves off the SAME print -- outcomes are deterministic functions of one draw. Shuffled StratifiedKFold leaks: siblings of a test strike sit in train. Minimum fix: GroupKFold by (symbol, expiry). See validation reconciliation |
| 48h-before reference point + edge check vs market price | train_outcome.py:57, 288-320 | quartile bins = XS | PORT (method) / RETRAIN (results) | "Does the model beat the market's own price" framing ports well; all numbers are Polymarket-specific |

### B6. decision/ layer

| Item | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| adjusted_edge = abs(pred) - spread/2; buffer 0.005 | decision/edge_calculator.py:25, 88-101 | no | RETRAIN | Own fill simulator already proved 0.005 optimistic by ~6-11x ON POLYMARKET; on ForecastEx the cost model changes shape: add flat $0.01/contract commission as an explicit term, spreads ~0.03 on the sampled pair. Re-measure the buffer |
| latest-snapshot book join | edge_calculator.py:139-171 | no | PORT | Remap to latest parquet row per conId; ADD a staleness gate -- "latest" delayed sweep can be 20+ min old |
| Kelly p, b from metrics_6h_v2.json profit sim | decision/position_sizer.py:26, 143-201 | XS (pooled cross-market distribution parameters) | RETRAIN | Global-p/b design carries; inputs (30-trade Polymarket sim) are void -- needs a new-venue profit sim or live trades before any position is sized |
| quarter-Kelly formula + caps/floor | position_sizer.py:28-32, 135-140 | no | KEEP (revisit floor) | Math venue-free; $5 floor vs $0.01/contract commission and multiplier 1 is a parameter to revisit, not a redesign |
| Rule 1 drawdown breaker (20%) | decision/risk_manager.py:183-195 | portfolio-level | KEEP | Venue-agnostic |
| Rule 2 cluster exposure cap (10%) | risk_manager.py:197-242 | XS (aggregates exposure across cluster) | PORT (new grouping) | Exactly what ForecastEx needs; grouping key becomes underlying symbol (arguably underlying-family: US GDP + 10 state GDPs are one macro bet). Correlation is TIGHTER on the new venue -- the cap matters more, not less |
| Rule 3 total exposure cap (35%) | risk_manager.py:244-270 | XS (portfolio sum) | KEEP | Venue-agnostic |
| Rule 4 max-concurrent, keep top-N by adjusted_edge | risk_manager.py:272-307 | XS (ranks concurrent signals) | KEEP (inherits edge retrain) | Ranking mechanism fine; its sort key is the retrained edge |
| paper_positions schema / risk_limits.json | risk_manager.py:58-104, 37-48 | no | PORT | Direct reuse; cluster column values change meaning with new grouping |

### B7. analysis studies -- methods vs conclusions

| Item | Location | XS? | Verdict | Notes |
|---|---|---|---|---|
| fill_simulator METHOD (book-through fill, mid_0-anchored decomposition, dense gating) | analysis/fill_simulator.py:149-239, 263-295 | pools orders per offset | PORT | Runs on recorder parquet nearly as-is; 60-min dense-gap criterion still sensible at 20-min cadence. New: `last` column can serve as a second fill trigger; still no trade sizes, so fills stay a lower bound |
| fill_simulator CONCLUSIONS (fill 3-6%, adverse +3.1-5.5 cents, buffer optimistic 6-11x) | analysis/results/fill_simulation.json | -- | RETRAIN (re-measure) | Pure venue empirics; ForecastEx has different flow (institutional/hedger), wide books, a coupon, a commission -- no number transfers |
| fill_simulator cohort (POLITICAL_REGEX) + MID_RANGE 0.05-0.95 | fill_simulator.py:76-80, 388 | cohort selection XS | RETIRE cohort / PORT pin-filter | Cohort becomes "all tradable YES conIds"; pin filter ports (deep-ITM/OTM strikes excluded -- expected and correct) |
| calibration_study (24h-before bins, Wilson-style SE, tradeability net of spread) | analysis/calibration_study.py:44-63, 188-262 | XS (bin stats = resolved-market distribution) | PORT method / RETRAIN data | Zero resolved ForecastEx markets exist in-house; needs months of settlement capture. MAKER_FEE=0 (line 63) must become $0.01/contract. Favorite-longshot magnitude is venue-specific; institutional-venue prior: smaller |
| monte_carlo tool | analysis/monte_carlo.py:139-267 | no | KEEP tool / RETRAIN inputs | Frictions parameterized (FILL_RATE 0.0597, ADVERSE_COST 0.03125 at lines 88-89 are Polymarket-measured). Rerun after a ForecastEx fill sim; add commission as a third friction, consider the coupon as negative-carry offset |
| monte_carlo conclusion ("break-even edge ~= adverse cost; bleed not blow-up at 6% fill") | analysis/results/monte_carlo.json | -- | RETRAIN | Structurally, break-even = adverse_cost + commission/entry_price carries; the numbers do not |
| prospective_evaluation (pre-registered LLM-vs-market verdict) | analysis/prospective_evaluation.py:50-61 | edge stats pool the disagreement cross-section | USER-DECISION | It cannot "migrate": changing venue voids the pre-registration by its own rules (constants frozen 2026-07-08). Options: (a) run to verdict on Polymarket data as a closed study, and/or (b) write a NEW pre-registration for LLM-vs-ForecastEx-macro (a much harder market -- counterparties are professional forecasters). Framework code (blinded status, Wilson gates) reusable either way |

### Stack B cross-sectional inventory (all suspect)

1. All 7 cluster features -- peer-distribution aggregates; config 100% dead Polymarket political IDs.
2. Confidence gate (train.py:749-768) -- median of pred_std over the candidate cross-section.
3. Kelly p/b (position_sizer) -- pooled cross-market trade distribution parameters.
4. Risk rules 2/3/4 -- cluster-sum, portfolio-sum, edge-rank over concurrent signals.
5. StratifiedKFold in train_outcome -- assumes independent outcomes; violated by strike-ladder settlement coupling (most dangerous silent transfer).
6. Calibration bins / tradeability table -- resolved-market distribution per price bucket.
7. Fill-sim per-offset aggregates -- order distribution pooled across markets.
8. Universe selectors (POLITICAL_REGEX in train.py / fill_simulator / calibration_study) -- define the training cross-section itself.
9. distance_from_round is NOT cross-sectional (fixed levels), despite appearances.

Cadence note (15-min SQLite -> 20-min parquet): lookback features degrade
negligibly; hard edges are (a) labels' +/-30-min window now tolerates one
missed sweep -- widen it; (b) count thresholds encode time spans -- renumber
x0.75. The 6h target is computable but should be redesigned around the
release calendar rather than mechanically retrained.

---

## Weather track vs ForecastEx climate contracts

### Critical correction: RCSEA/RCSTH are NOT rainfall

The `RC*` prefix is **Regional CPI**, not rainfall (from IND longNames:
RCSEA = "Seattle CPI", RCSTH = "Southern US CPI", plus 15 more RC* city and
regional CPIs, all chainless except RCSEA and RCSTH). A grep of every
longName for rain/precip/snow/weather returns **zero hits: the venue lists
no precipitation and no daily city-weather product of any kind.** RCSTH is
the only listed CPI-family instrument with a live ~monthly cadence (148 OPT
rows, 5 expiries); RCSEA has 16 rows, 1 expiry.

### Climate universe (snapshot 2026-08-16)

| Symbol | Product | OPT rows | Expiries | Cadence |
|---|---|---|---|---|
| GT | Global Temperature (annual anomaly, degC) | 122 | 5 (2027-2036) | annual, plus long-dated |
| GTM | Monthly Avg Global Temperature | 122 | 5 (~monthly; front month already rolled vs snapshot) | ~monthly -- the only short-horizon climate product |
| GTTA / GTTM | Annual / Monthly Temperature Threshold | 38 / 26 | 2031-2046 / 2031-2041 | first resolution 2031 -- pure scenario bets |
| ACD | Atmospheric CO2 (ppm) | 70 | 4 (Jan 2027-2036) | annual |
| USCE | US CO2 Emissions (Mt) | 220 | 5 (2027-2036) | annual; energy accounting, not meteorology |
| HCAB / HLF | Atlantic hurricane count / US landfall count | 14 / 30 | 1 each (Dec 2026) | one resolution/year |
| HCAT4 | Cat-4 US hurricane (binary) | 8 | 4 (2026-2027) | seasonal |
| MACD, GCE, HLFRE, MHCMA | listed IND, no chain | 0 | -- | watch for future listings (MACD monthly CO2 would be the best-cadence climate product if chained) |

**Headline mismatch, stated plainly: the weather track's core asset -- EMOS
post-processing of 0-6-day NWP ensemble forecasts for city stations -- has
no target contract on ForecastEx.** No daily temperature, no city weather,
no rainfall. Closest instruments are monthly/annual global-mean anomalies,
annual CO2, and seasonal hurricane counts -- different horizons, data
sources, and resolution oracles.

### Weather-track feature audit

Cross-sectional finding for this scope: **zero.** No weather-track feature
uses the distribution across markets as a signal -- everything is
per-station/per-market; the only pooling is in evaluation
(calibration/go-live), which aggregates resolved predictions without ranking
markets against each other.

| Feature | Location | Verdict | Notes |
|---|---|---|---|
| weather_edge (ask = mid + spread/2, edge = p - ask - fee) | weather/edge.py:24 | PORT | Arithmetic valid on ForecastEx YES quotes; swap quote source to recorder parquet by con_id; set fee = $0.01/contract commission |
| evaluate_weather_edge gates (min_edge 0.05, price 0.02-0.98) | weather/edge.py:34 | PORT | Logic ports; threshold VALUES need re-tuning on venue spreads (USER-DECISION) |
| Kelly/half-Kelly sizing in signal | weather/edge.py:57 | PORT | Identical binary payoff structure |
| GoLiveCriteria / evaluate_go_live (90d, 200 buckets, ECE<=0.08, slope 0.9-1.1, beat market Brier) | weather/paper_criteria.py:12, :32 | KEEP | Anti-optional-stopping discipline is venue-agnostic and the track's most transferable asset; the 200-buckets-in-90-days numbers are unreachable on monthly/annual cadence -- re-scope thresholds per strategy (USER-DECISION) |
| fit_emos / predict_emos (CRPS-minimum Gaussian post-processing) | weather/emos.py:55, :94 | RETRAIN | Machinery reusable for any ensemble->scalar target (e.g. GTM via seasonal-forecast ensembles); all fitted params in models/emos/*.json target Polymarket city markets -> those RETIRE |
| crps_gaussian | weather/emos.py:16 | KEEP | Closed-form scoring, target-independent |
| Bucket, p_bucket_gaussian, p_above_gaussian | weather/buckets.py:21, :42, :52 | PORT | ForecastEx strike maps directly: YES == Bucket(lo=strike, hi=None) / p_above_gaussian(mu, sigma, strike) |
| truncate_whole_degree / contains_truncated | weather/buckets.py:11, :26 | RETIRE | Polymarket whole-degF settlement artifact; ForecastEx strikes are decimal |
| p_bucket_from_members / p_bucket_from_quantiles | weather/buckets.py:59, :66 | KEEP | Generic CDF machinery |
| xmodel_prevruns_v1 serve features (build_xmodel_features) | weather/serve_features.py:212 | RETIRE (ForecastEx scope) | Exists solely to price daily city-temp markets; no target contract on venue. Keep only if a Polymarket track continues |
| load_emos_for_feature_set + feature_set stamping | weather/serve_features.py:80; weather/model_store.py:71 | KEEP | Train/serve parity guard transfers to any model family |
| Label priority (iem_cli > nws_cli > wunderground_api > iem_asos) | weather/training.py:70; weather/storage.py:97 | RETRAIN | Climate contracts resolve on different authorities (global temp series, NOAA GML CO2, NHC/HURDAT, EIA, BLS) -- new label collectors required; the multi-source-priority + finalized-flag PATTERN transfers |
| Finalization gates (finalized=1, >=72h, usable_for_training) | weather/finalization.py:16, :28 | KEEP | Pattern transfers; revision windows must be re-sized (HURDAT best-track reanalysis takes months; CPI/GDP revise) |
| weather_markets schema (station_icao, bucket_lo/hi, clob_token_ids) | weather/storage.py:36 | PORT | Map to con_id/symbol/expiry/strike/right -- recorder parquet already carries exactly that schema |
| resolve_quote / live_clob_quote / latest_quote | weather/market_quotes.py:170, :115, :58 | PORT | Replace with recorder parquet reads keyed by con_id; live-CLOB fallback has NO equivalent until an ib_async quote path exists |
| evaluate_binary_probs / CalibrationReport / reliability_slope | weather/calibrate.py:54, :18, :39 | KEEP | Venue-agnostic diagnostics |
| fit_quantile_models (ML quantile track) | weather/ml_quantile.py:43 | RETRAIN | Machinery generic, features/data all new |
| Paper loop orchestration | scripts/run_weather_paper.py:1 | PORT | Skeleton (features -> EMOS -> edge -> persist -> calibrate -> go-live) reusable; every I/O adapter changes |
| Open-Meteo client + licensing gates | weather/open_meteo.py:100, :86 | RETIRE (ForecastEx scope) | Produces none of the venue's climate targets |
| Station registry | weather/stations.py:40 | RETIRE (ForecastEx scope) | No station-level contracts on venue |
| Obs/history collectors (observations_to_daily, previous_runs_payload_to_daily, ensemble_daily_from_open_meteo) | weather/obs_daily.py:36; weather/history.py:70; weather/daily_agg.py:43 | RETIRE (ForecastEx scope) | Keep only for a continuing Polymarket track |

### Capability -> contract transfer (honest)

| Existing capability | Best-fit contracts | Transfer quality | Genuinely new |
|---|---|---|---|
| EMOS post-processing of ensembles | GTM (monthly global anomaly, ~6 expiries/yr) | Structural fit: seasonal-forecast ensembles (ECMWF SEAS5/NMME via Copernicus CDS) -> monthly-mean anomaly is exactly an ensemble->scalar EMOS problem; SEAS5 hindcasts (1993-2016+) give decades of training data on day one -- better than the Previous-Runs cold start | CDS API; the exact resolution series per contract must be VERIFIED (GISTEMP vs Copernicus vs NOAA differ by ~0.1 degC -- material at 0.05 strike granularity); baseline conventions per strike |
| Trend/seasonal time-series fit + calibration discipline | ACD (annual CO2) | Mauna Loa CO2 is among the most predictable geophysical series (trend + seasonal, ~+/-0.5 ppm); Jan-2027 expiry = 2026 annual mean, mostly observed by Q4 -- sharp, checkable probabilities | NOAA GML monthly feed; only ~9 near-dated strikes, liquidity unknown |
| Count modeling (user's Poisson background) | HCAB/HLF/HCAT4 | Negative-binomial seasonal count models conditioned on ENSO/SST are standard; mid-season the count is partially observed ("observed + remaining-season" model) | NHC/HURDAT labels, CSU/NOAA outlook covariates; ONE resolution/year -- a ladder resolves as ~7-15 CORRELATED binaries, effective sample ~= 1/season; go-live-by-calibration is impossible on hurricanes alone |
| Long-dated scenario pricing | GT 2031/2036, GTTA/GTTM 2031-2046 | Not a calibration problem -- first resolutions 2031; no feedback loop inside any reasonable horizon | USER-DECISION: discretionary research bets only, outside the systematic framework |
| Go-live criteria discipline | all of the above | Transfers as PROCESS; 200-buckets/90-days bar was designed for daily markets; monthly climate cadence yields ~6-12 independent resolutions/yr | Thresholds redefined per cadence (USER-DECISION) |

### Question set vs universe (18 questions)

All 18 questions in `data/questions/sample_questions.yaml` map to **nothing
tradable** on ForecastEx -- verified per-question by Auditor C. The venue is
exclusively macro + climate; no equity, crypto, corporate-event, AI, space,
or regulation products. By category (all NO): AI releases x3, AI milestones
x3, hardware x3 (incl. the one question with a Polymarket CID), crypto x2,
space x2, regulation x2, enterprise x2 (nvidia market-cap only very loosely
shares a growth factor with US recession contracts -- not an
implementation), autonomous x1.

Auditor C's verdict on the artifact: RETIRE for IBKR trading; KEEP as a
Polymarket research corpus is contingent on retargeting Option 3 -- a user
decision, not made here. The YAML schema itself (id/deadline/
resolution-criteria/search_keywords) is venue-neutral and PORTs to macro
questions.

---

## Data gaps -- features needing data the recorder does not capture yet

| Missing data | What it blocks | Path / next step |
|---|---|---|
| Trade volume / trade prints | volume_24h (1 of 19 Stack B FEATURE_KEYS); Stack A market_volume_24h, market_total_volume, market_liquidity, volume_intensity + its Bayesian rule and ML weight; opportunity_score liquidity half; any VPIN-like flow feature | No volume column, no trade prints. Auditor A: RETIRE, no equivalent. Auditor B: RETIRE *until* the recorder grows an IBKR delayed-volume-tick column (only noted future path). `last` exists (price only) -- fill simulation stays a lower bound |
| Full-book depth | True bid_depth/ask_depth (recorder has top-of-book bid_size/ask_size only) | reqMktDepth feed EXISTS per venue_semantics Q4; level count/quality unmeasured -- Stage 5 task |
| Settlement / resolution capture | generate_outcome_labels, calibration_study, ensemble calibration history, any outcome model, go-live records | Build a settlement/expiry capture job (YES leg pays 1.00 + coupon); months of accrual before defensible calibration |
| Real-time (non-delayed) quotes | Live edge_calculator book joins; live-CLOB-equivalent quote fallback in weather/market_quotes.py | Everything inherits ~15-20-min-stale delayed data -- acceptable for research features, NOT for live edge; live-feed subscription cost unknown (docs/operating_costs.md) |
| Coupon rate | Accurate carry/fee modeling; monte_carlo negative-carry term | UNVERIFIED (venue_semantics Q5); verify from statements/accruals on a real position, never hardcode, re-verify <=30 days |
| Macro release calendars (BLS/BEA/FOMC; NHC advisories) | Fine-grained scheduled_resolution_time (currently expiry-only, src/venues/ibkr/adapter.py:314); release-calendar-aware label horizon; release-calendar scheduler | Open item; data source choice is part of the retargeting USER-DECISION |
| New-domain label/feature sources (FRED/ALFRED vintages, GDPNow/Cleveland nowcasts, Copernicus CDS SEAS5, NOAA GML CO2, NHC/HURDAT, exact per-contract resolution series) | Everything in retargeting Options 1 and 2 | Depends on which option is chosen; ALFRED vintage-correctness and resolution-series verification are known traps |

---

## Validation discipline: what exists vs what does not

Stated honestly, because the migration spec's validation language could be
misread as describing this repo.

**Does NOT exist in this repo (net-new work if wanted):**

- Purged / embargoed k-fold CV -- nowhere.
- Walk-forward evaluation harness -- nowhere (the temporal split is a single
  60/20/20 cut, not rolling walk-forward).
- Triple-barrier labeling -- nowhere (labels are fixed-horizon 6h deltas and
  terminal outcomes).
- Sample weighting (uniqueness / overlap weights) -- nowhere.
- Deflated Sharpe ratio / PBO-style multiple-testing correction -- nowhere.

These are listed as net-new backlog items, not as things being "ported".

**DOES exist, and is the discipline actually protecting the system:**

| Mechanism | Location | Migration status |
|---|---|---|
| Temporal 60/20/20 row-count split | model/train.py:308-335 | KEEP (Auditor B) -- venue-agnostic, robust to sparse history |
| Causal feature computation ("latest <= cutoff" lookups; features never read the future) | features/market_features.py:281-285 and throughout | PORT -- same causal lookups on 20-min sweeps |
| StratifiedKFold (shuffled) CV | model/train_outcome.py:374 | RETRAIN (redesign CV) -- Auditor B flags this as the single most dangerous silent transfer: strike-ladder siblings of a test contract sit in train because all strikes of one (underlying, expiry) settle off the same print. Minimum fix GroupKFold by (symbol, expiry). This is the ONE existing mechanism that must CHANGE -- and the change is a strengthening, not a weakening |
| Pre-registered prospective evaluation (frozen constants, blinded status, Wilson gates) | analysis/prospective_evaluation.py:50-61 | USER-DECISION -- venue change voids the pre-registration by its own rules (frozen 2026-07-08); close it on Polymarket data and/or write a new registration for ForecastEx. Framework code reusable |
| Weather go-live criteria (90d window, 200 resolved buckets, ECE<=0.08, slope band, beat-market Brier) | weather/paper_criteria.py:12, :32 | KEEP process; re-scope thresholds per cadence (USER-DECISION) |
| Anti-overfitting machinery: learning-curve sweep, feature-stability check, 5-seed ensemble | model/train.py:587-722 | KEEP |

**The binding constraint: the new dataset is SMALLER in effective samples,
not larger.** A macro release resolves an entire strike ladder at once, so
independent samples = number of releases (~12-20/yr across Option-1
underlyings, ~6-12/yr climate, ~1/yr hurricanes) -- roughly 12-24 months to
a defensible calibration record. On a dataset that small, the existing
discipline (temporal split, pre-registration, go-live gates, seed ensembles,
count-not-contract effective-sample accounting) must **not** be weakened to
"get results faster". Adding the advanced techniques above is optional
backlog; keeping the current discipline intact is mandatory. Concretely:
never report per-contract sample counts where per-release counts are the
truth; never let a new CV scheme reintroduce the ladder leak StratifiedKFold
already has.

---

## USER DECISIONS

Everything in this section is **flagged, not decided**. Nothing below has
been implemented, defaulted, or assumed elsewhere in this document.

### D1. Question-set retargeting (the big one)

The 18-question corpus maps to nothing tradable on ForecastEx. Three
options, per Auditor C:

**Option 1 -- Macro-calendar strategy** (UNR, RGDP, state GDQ*,
international GDP, RCSTH CPI, USR/CAREC recession).
The venue's structural center of gravity: scheduled release times,
monthly-to-quarterly cadence, richest chains (RGDP 234 rows/7 expiries, UNR
122/6, SGGDP 204/8, HKGDP 178/8, ten state-GDP ladders, RCSTH monthly).
- Carries over: edge arithmetic + Kelly (weather/edge.py), calibration /
  go-live discipline, src/evaluation metrics, question YAML schema, recorder
  parquet as quote store, risk-manager check structure.
- New: FRED/ALFRED (vintage-correct history), BEA/BLS release calendars,
  nowcasts (Atlanta Fed GDPNow, Cleveland Fed inflation), consensus surveys,
  a release-calendar scheduler.
- Data accumulation: nowcast-vs-market divergence checks possible
  IMMEDIATELY (nowcasts have public real-time archives). Honest caveat: one
  release resolves a whole ladder -- ~12-20 independent samples/yr, so
  ~12-24 months to a defensible calibration record.
- Con: entirely new domain skill; vintage/revision handling is a well-known
  trap.

**Option 2 -- Climate strategy** (GTM monthly + ACD annual + hurricane
counts), leveraging the weather/EMOS discipline.
Closest to existing skill in PROCESS (ensemble post-processing, bucket
probabilities, pre-registered go-live), though not in data.
- Carries over: EMOS machinery, p_above_gaussian bucket mapping,
  calibration/go-live, paper-loop skeleton, feature-set stamping.
- New: Copernicus CDS (SEAS5 hindcasts + live seasonal forecasts; ERA5
  monthlies), the exact resolution series per contract (verify from
  ForecastEx specs -- ~0.1 degC baseline differences matter at 0.05-strike
  granularity), NOAA GML CO2 feed, NHC/HURDAT + CSU outlooks.
- Data accumulation: SEAS5 hindcasts (~25 yrs) mean a hindcast-calibrated
  GTM model within weeks; live paper record accrues at only ~6
  resolutions/yr -- go-live thresholds need re-scoping.
- Con: thin listed chains (GTM 122 rows, ACD 70), unknown liquidity on
  delayed 20-min quotes; hurricanes give ~1 effective sample/year.

**Option 3 -- Two-venue split**: keep Stack A (LLM research / 18-question
pipeline) on Polymarket research-only; trade IBKR solely on systematic
signals from Option 1 and/or 2.
- Carries over: everything, unchanged, on both sides; recorder keeps
  accumulating IBKR history meanwhile (already running).
- New: nothing immediately; deferred choice.
- Pro: no forced retirement of the research corpus; the maker-fill
  adverse-selection finding already bounds Polymarket expectations (stays
  paper/research). Con: attention split across two venues and two
  codepaths; risk neither side reaches go-live depth.

### D2. Modeling-design decisions flagged by the auditors

| # | Decision | Flagged by | Where |
|---|---|---|---|
| 1 | Sign-polarity mapping per underlying family: sentiment weights, mention-volume likelihoods, FEATURE_WEIGHTS all hardcode "positive/more news -> YES"; on threshold contracts (UNR, recession, hurricanes) the mapping is contract-specific and often inverted. Decide the mapping design, then RETRAIN | A | sentiment.py, bayesian_predictor.py, classical_ml.py |
| 2 | Does mention_acceleration carry signal for macro/climate at all? (Coverage accelerates on schedule regardless of outcome) | A | frequency.py:17-25 |
| 3 | Source-diversity likelihood: diversity -> YES plausible for events, dubious for thresholds | A | bayesian_predictor.py:74-76 |
| 4 | Trade deep-ITM/OTM strikes or not? Governs the LLM [0.05, 0.95] clamp + "avoid extremes" prompt rule, and interacts with filter_actionable's 0.10-0.90 band | A | llm_predictor.py:154-157; market_selector.py:137-162 |
| 5 | min_edge threshold (currently 0.05) re-derived from ForecastEx spread + commission | A | edge_finder.py:19 |
| 6 | distance_from_round: does round-number psychology exist on an institutional macro venue? Natural analog: distance-to-adjacent-strike | B | market_features.py:310-314 |
| 7 | DIRECTION_TOLERANCE dead zone (0.005) vs minTick 0.01 (half-tick mid quanta) | B | labels.py:48 |
| 8 | Trade threshold +/-0.02 (= 2 minTicks, < one observed spread) reset relative to spread + commission | B | train.py:91-92 |
| 9 | Ladder cluster aggregates: replace cluster_avg_price / cluster_price_dispersion with implied-CDF summaries (ladder median strike, CDF steepness)? | B | correlation_features.py:201-208 |
| 10 | prospective_evaluation disposition: close on Polymarket as a registered study, write a new ForecastEx pre-registration, or both | B | analysis/prospective_evaluation.py |
| 11 | Go-live thresholds re-scoped per cadence (200 buckets/90 days is unreachable at ~6-12 climate resolutions/yr) | C | weather/paper_criteria.py |
| 12 | Weather edge-gate threshold values (min_edge 0.05, price band 0.02-0.98) re-tuned on venue spreads | C | weather/edge.py:34 |
| 13 | Long-dated scenario contracts (GT 2031/2036, GTTA/GTTM 2031-2046): discretionary research bets outside the systematic framework, or skip | C | universe |
| 14 | Cluster-exposure grouping key: underlying symbol vs underlying-family (US GDP + 10 state GDPs as one macro bet) | B | risk_manager.py:197-242 |
| 15 | Position floor: $5 min ticket vs $0.01/contract commission and multiplier 1 -- parameter revisit | B | position_sizer.py:135-140 |
| 16 | scheduled_resolution_time source: contract expiry only (current) vs wiring macro release calendars (BLS/BEA/FOMC timestamps) -- data-source and scheduling decision tied to D1 | spec / adapter comment | src/domain/models.py:39; src/venues/ibkr/adapter.py:310-314 |
| 17 | Question-set artifact disposition (retire vs keep as Polymarket research corpus) -- follows from D1 Option 3 | C | data/questions/sample_questions.yaml |

### Conflicts between auditors (for the record)

- **volume_24h**: Auditor A -- RETIRE, "no equivalent exists"; Auditor B --
  "RETIRE (until recorder captures it)", noting an IBKR delayed-volume-tick
  recorder column as a possible future path. Same verdict today; only the
  future disposition differs.
- No other verdict conflicts found; A's PORT of Stack-A market-price
  features and B's PORT of price_now agree (both cite mid-sum ~= 1.00 and
  prefer real NO quotes over the 1-YES approximation), and both
  independently flag the strike-ladder extremes problem and the
  commission-in-edge requirement.
