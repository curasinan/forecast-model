# GTM model report -- SEAS5 + EMOS for ForecastEx monthly global temperature

Fitted 2026-08-23. Code: `climate/bridge.py`, `climate/dataset.py`,
`climate/emos.py`, `climate/fit.py`, `climate/ladder.py`, `climate/paper.py`,
`climate/paper_criteria.py`; scripts `fit_gtm_emos.py`, `run_gtm_paper.py`.
Artifacts (outside the repo): `%LOCALAPPDATA%\climate_data\models\gtm_emos.json`
(operational, trend variant), `..._notrend.json`, `reports\gtm_emos_diagnostics*.{md,json}`,
`paper\gtm_signals.json`. Research only -- nothing here places orders.

## 1. Data provenance

| Input | Source | Coverage | Rows |
|---|---|---|---|
| Label: NOAA NCEI monthly global land+ocean anomaly vs 1901-2000 (the GTM settling series, strictly >) | `labels_ledger.parquet`, series `gtm`, first-print rows | 1850-01 .. 2026-07 | 2,119 |
| Forecast: SEAS5 (system 51) per-member monthly-mean 2m temperature, cos-lat global mean | `forecasts/seas5_global_mean.parquet` | inits 1993-01 .. 2026-08, leads 1-6; 25 members before 2017, 51 after | 76,860 |
| Reference: ERA5 monthly global-mean 2m temperature | `reference/era5_global_mean.parquet` | 1993-01 .. 2025-12 | 396 |
| Training frame (one row per init x lead) | `climate.dataset.build_training_frame` | 2,388 rows, 2,377 labelled | |

Empirical conventions established by the build (see module docstrings):
SEAS5 `leadtime_month=1` is the initialisation month itself; a GTM contract's
label month (`GTM_MMYY_...`) is the TARGET month (NOAA publishes month M in
the first half of M+1; the contract's real expiration is ~6 weeks after M).

## 2. ERA5 -> NOAA bridge

`noaa_C = alpha[month_of_year] + beta * era5_K`, fit on 396 overlapping
months: beta = 0.904 degC/K, R^2 = 0.983, residual sd = 0.036 degC.
(The bridge absorbs the different baselines and the seasonal cycle ERA5
retains and NOAA removes; its residual is well under one 0.1-degC strike.)

## 3. Model variants -- out-of-sample, expanding window (test years 2001-2026,
refit each year on strictly earlier targets)

Gaussian EMOS per lead: mu = a + b*fc_mean (+ c_t*(year-2010) if trend),
sigma^2 = c + d*fc_var, fit by minimum CRPS.

| lead | n | CRPS no-trend | CRPS trend | CRPS trend-clim | CRPS clim | skill vs trend-clim (no-trend / trend) | cover50 / cover90 (trend) |
|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 301 | 0.0450 | **0.0403** | 0.0786 | 0.1816 | 0.427 / **0.488** | 0.508 / 0.880 |
| 2 | 301 | 0.0532 | **0.0487** | 0.0784 | 0.1812 | 0.322 / **0.378** | 0.532 / 0.900 |
| 3 | 302 | 0.0556 | **0.0502** | 0.0783 | 0.1815 | 0.289 / **0.359** | 0.543 / 0.891 |
| 4 | 303 | 0.0575 | **0.0516** | 0.0782 | 0.1819 | 0.265 / **0.340** | 0.548 / 0.878 |
| 5 | 304 | 0.0605 | **0.0543** | 0.0780 | 0.1823 | 0.224 / **0.304** | 0.543 / 0.882 |
| 6 | 305 | 0.0659 | **0.0579** | 0.0778 | 0.1827 | 0.154 / **0.257** | 0.538 / 0.859 |

PIT (10 bins, pooled OOS): no-trend variant is strongly right-skewed
(top decile 20.0%, bottom 3.2% -- the warming trend is under-forecast in
later years); trend variant is close to flat (top 13.4%, bottom 7.2%,
interior 6.9-12.2%). Pooled coverage, trend variant: 50% -> 0.535,
90% -> 0.882 (slightly under-dispersed at 90%).

**Chosen: the trend variant** (better CRPS at every lead, much better
calibration). It is the operational `gtm_emos.json`. The residual upper-tail
excess reflects the record-warm 2023-2025 and should be re-checked as those
years enter the training set.

What this supports: SEAS5 post-processed this way beats trend-adjusted
climatology out of sample at every lead, strongly at leads 1-3 (skill
0.36-0.49) and modestly at lead 6 (0.26). What it does NOT yet support:
any live trading. The skill numbers are hindcast/expanding-window, the labels
for the backfill are current-vintage (true first prints accrue only from
now), the quotes are 20-minute delayed, and no paper record exists.

## 4. Gates (`climate/paper_criteria.py`)

- Hindcast model gate (skill vs trend-clim > 0 at >= 3 leads, coverage
  50% in [0.35, 0.65], 90% in [0.75, 0.98], n >= 24 per lead): **PASSED**
  for the trend variant at all six leads.
- Live paper gate (>= 365 paper days AND >= 12 resolved months): **NOT
  MET** (0 / 0). `run_gtm_paper.py` enforces this gate by default; the
  hindcast-only verdict requires `--hindcast-gate-only`.
- Edge floor: $0.01 commission + 1 tick = 0.02; price band 0.05-0.95.

## 5. Ladder snapshot, 2026-08-23 (delayed quotes; research only)

52 GTM contracts quoted across target months 2026-08/09/10/12 and 2027-01;
23 pass the research edge gate. Two observations that demand caution:

- **December 2026**: the model's lead-5 forecast is mu = 1.44, sigma = 0.14
  degC -- an El Nino-like warming path (Aug 1.33 -> Nov 1.48). The market
  prices P(>1.2) at ~0.35 vs model 0.96, on live 3-cent quotes. A ~60-point
  gap is far larger than any honest edge estimate: treat it as "real
  information plus single-scenario over-confidence", to be adjudicated by the
  paper record, not traded.
- Recent months as a sanity check: predicted vs NOAA print -- 2026-03 1.19 vs
  1.21, 2026-05 1.10 vs 1.09, 2026-07 1.23 vs 1.18 (errors ~0.02-0.05 at
  short leads, consistent with the CRPS).
- August/September 2026 carry an older coarse ladder (-1.5 .. 3.5 in 0.5
  steps); far strikes show ask-only 0.02 quotes (dead); harmless.

## 6. Caveats and next steps

1. First-print vintage: backfill labels are current vintage by necessity;
   Rule 603(e) settles on first print. The live ledger captures first prints
   going forward -- re-fit periodically and compare.
2. SEAS5 real-time forecasts arrive ~the 5th-6th of the init month; the
   operational loop must use the latest init available, never a later one.
3. Over-confidence risk at leads 4-6 in regime-change months (ENSO onset):
   consider inflating sigma for those leads until the paper record says
   otherwise; the 90% coverage of 0.86-0.88 already hints at it.
4. Next build: schedule `collect_climate_labels.py` daily, run
   `run_gtm_paper.py` after each SEAS5 release and each recorder sweep to
   accumulate the paper book, and rewire the executor onto `VenueAdapter`
   before any paper ORDERS (Stage 7, blocked on paper-account provisioning).
