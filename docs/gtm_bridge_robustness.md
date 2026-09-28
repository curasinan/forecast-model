# GTM hindcast skill with the ERA5→NOAA bridge fit inside each window (audit H4)

Generated (UTC): 2026-09-28T15:14:58Z  
Command: `python scripts/study_bridge_window.py --out docs/gtm_bridge_robustness.md --json-out docs/gtm_bridge_robustness.json`  
Data root (read-only): `~/quant_data/climate_data`

## Headline

Honest-bridge hindcast gate (trend variant): **PASS** — `evaluate_hindcast_go_live` with the default, unchanged criteria on the in-window-bridge summary.

- Operational variant: `trend` — models/gtm_emos.json (trend=True).
- Same variant with the production full-history bridge (leaky): **PASS**.
- Per-lead skill delta vs trend climatology (honest − leaky), `trend`: L1 -0.008, L2 -0.008, L3 -0.006, L4 -0.003, L5 +0.002, L6 +0.003.
- Other variant `notrend`: honest gate **PASS**, leaky gate **PASS**.

## Inputs

- seas5_rows: 76860
- seas5_inits: 1993-01 .. 2026-08
- seas5_leads: [1, 2, 3, 4, 5, 6]
- era5_months: 1993-01 .. 2025-12 (396)
- noaa_gtm_months: 1850-01 .. 2026-08 (2120)
- era5_noaa_overlap: 1993-01 .. 2025-12 (396)

## Variant `notrend`

- Test years 2001–2026, leads [1, 2, 3, 4, 5, 6], min_train_targets 24, runtime 3.0s.
- Honest-bridge gate: **PASS**; leaky (production) gate: **PASS**.

| lead | n leaky | n honest | CRPS EMOS leaky | CRPS EMOS honest | CRPS trend-clim | skill vs trend (leaky) | skill vs trend (honest) | delta (honest − leaky) | cover50 honest | cover90 honest |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 302 | 302 | 0.0449 | 0.0457 | 0.0789 | 0.431 | 0.421 | -0.009 | 0.457 | 0.864 |
| 2 | 302 | 302 | 0.0531 | 0.0541 | 0.0787 | 0.326 | 0.312 | -0.014 | 0.510 | 0.897 |
| 3 | 302 | 302 | 0.0556 | 0.0566 | 0.0783 | 0.289 | 0.278 | -0.012 | 0.467 | 0.881 |
| 4 | 303 | 303 | 0.0575 | 0.0580 | 0.0782 | 0.265 | 0.258 | -0.007 | 0.485 | 0.875 |
| 5 | 304 | 304 | 0.0605 | 0.0604 | 0.0780 | 0.224 | 0.226 | +0.002 | 0.447 | 0.872 |
| 6 | 305 | 305 | 0.0659 | 0.0655 | 0.0778 | 0.154 | 0.159 | +0.005 | 0.443 | 0.862 |

## Variant `trend` (operational)

- Test years 2001–2026, leads [1, 2, 3, 4, 5, 6], min_train_targets 24, runtime 5.0s.
- Honest-bridge gate: **PASS**; leaky (production) gate: **PASS**.

| lead | n leaky | n honest | CRPS EMOS leaky | CRPS EMOS honest | CRPS trend-clim | skill vs trend (leaky) | skill vs trend (honest) | delta (honest − leaky) | cover50 honest | cover90 honest |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 302 | 302 | 0.0402 | 0.0408 | 0.0789 | 0.491 | 0.483 | -0.008 | 0.520 | 0.871 |
| 2 | 302 | 302 | 0.0487 | 0.0493 | 0.0787 | 0.382 | 0.374 | -0.008 | 0.523 | 0.901 |
| 3 | 302 | 302 | 0.0502 | 0.0507 | 0.0783 | 0.359 | 0.352 | -0.006 | 0.553 | 0.884 |
| 4 | 303 | 303 | 0.0516 | 0.0518 | 0.0782 | 0.340 | 0.337 | -0.003 | 0.531 | 0.875 |
| 5 | 304 | 304 | 0.0543 | 0.0542 | 0.0780 | 0.304 | 0.305 | +0.002 | 0.530 | 0.882 |
| 6 | 305 | 305 | 0.0579 | 0.0577 | 0.0778 | 0.257 | 0.259 | +0.003 | 0.528 | 0.862 |

## Bridge drift across windows

Production full-history bridge: beta 0.9042, R² 0.9830, resid sd 0.0356, n 396, 1993-01..2025-12.

The leak's size is the shift of `fc_mean_C` (window bridge − full bridge, degC) on the rows scored for that test year (alpha and beta trade off against the ~287 K ERA5 level, so raw alpha differences are not informative). `fc_sd_C` scales by beta_window / beta_full.

| test year | fit window | n months | beta | beta − full | R² | resid sd | scored rows | mean Δ fc_mean_C | mean abs Δ fc_mean_C |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2001 | 1993-01..2000-12 | 96 | 0.8814 | -0.0227 | 0.9475 | 0.0346 | 72 | +0.0012 | 0.0084 |
| 2002 | 1993-01..2001-12 | 108 | 0.8730 | -0.0311 | 0.9476 | 0.0340 | 72 | -0.0014 | 0.0077 |
| 2003 | 1993-01..2002-12 | 120 | 0.8591 | -0.0451 | 0.9529 | 0.0332 | 72 | -0.0041 | 0.0090 |
| 2004 | 1993-01..2003-12 | 132 | 0.8574 | -0.0468 | 0.9567 | 0.0322 | 72 | -0.0025 | 0.0092 |
| 2005 | 1993-01..2004-12 | 144 | 0.8536 | -0.0505 | 0.9557 | 0.0322 | 72 | -0.0080 | 0.0113 |
| 2006 | 1993-01..2005-12 | 156 | 0.8462 | -0.0580 | 0.9590 | 0.0320 | 72 | -0.0070 | 0.0093 |
| 2007 | 1993-01..2006-12 | 168 | 0.8432 | -0.0610 | 0.9597 | 0.0318 | 72 | -0.0074 | 0.0098 |
| 2008 | 1993-01..2007-12 | 180 | 0.8388 | -0.0654 | 0.9602 | 0.0318 | 72 | -0.0003 | 0.0071 |
| 2009 | 1993-01..2008-12 | 192 | 0.8462 | -0.0580 | 0.9582 | 0.0323 | 72 | -0.0069 | 0.0083 |
| 2010 | 1993-01..2009-12 | 204 | 0.8544 | -0.0498 | 0.9548 | 0.0336 | 72 | -0.0104 | 0.0122 |
| 2011 | 1993-01..2010-12 | 216 | 0.8533 | -0.0508 | 0.9582 | 0.0333 | 72 | -0.0025 | 0.0051 |
| 2012 | 1993-01..2011-12 | 228 | 0.8536 | -0.0506 | 0.9584 | 0.0326 | 72 | -0.0050 | 0.0060 |
| 2013 | 1993-01..2012-12 | 240 | 0.8522 | -0.0520 | 0.9587 | 0.0324 | 72 | -0.0077 | 0.0085 |
| 2014 | 1993-01..2013-12 | 252 | 0.8523 | -0.0518 | 0.9571 | 0.0328 | 72 | -0.0098 | 0.0100 |
| 2015 | 1993-01..2014-12 | 264 | 0.8646 | -0.0396 | 0.9561 | 0.0338 | 72 | -0.0127 | 0.0127 |
| 2016 | 1993-01..2015-12 | 276 | 0.8836 | -0.0205 | 0.9591 | 0.0352 | 72 | -0.0095 | 0.0097 |
| 2017 | 1993-01..2016-12 | 288 | 0.8901 | -0.0141 | 0.9666 | 0.0358 | 72 | -0.0057 | 0.0063 |
| 2018 | 1993-01..2017-12 | 300 | 0.8952 | -0.0090 | 0.9700 | 0.0356 | 72 | -0.0036 | 0.0048 |
| 2019 | 1993-01..2018-12 | 312 | 0.9004 | -0.0038 | 0.9710 | 0.0355 | 72 | -0.0024 | 0.0044 |
| 2020 | 1993-01..2019-12 | 324 | 0.9067 | +0.0025 | 0.9738 | 0.0354 | 72 | -0.0001 | 0.0037 |
| 2021 | 1993-01..2020-12 | 336 | 0.9092 | +0.0051 | 0.9763 | 0.0352 | 72 | +0.0003 | 0.0032 |
| 2022 | 1993-01..2021-12 | 348 | 0.9095 | +0.0053 | 0.9767 | 0.0349 | 72 | +0.0004 | 0.0032 |
| 2023 | 1993-01..2022-12 | 360 | 0.9108 | +0.0067 | 0.9771 | 0.0348 | 72 | +0.0024 | 0.0040 |
| 2024 | 1993-01..2023-12 | 372 | 0.9104 | +0.0062 | 0.9794 | 0.0355 | 72 | +0.0036 | 0.0036 |
| 2025 | 1993-01..2024-12 | 384 | 0.9082 | +0.0041 | 0.9823 | 0.0353 | 72 | +0.0018 | 0.0018 |
| 2026 | 1993-01..2025-12 | 396 | 0.9042 | +0.0000 | 0.9830 | 0.0356 | 18 | +0.0000 | 0.0000 |

## Method

- **Leaky** (production): one bridge fit on the full ERA5/NOAA overlap, then `expanding_window_scores` over the test years — exactly what `climate.fit.fit_gtm` computes.
- **Honest**: for each test year Y, `Bridge.fit(start=None, end=f"{Y-1}-12")`, the frame rebuilt with `build_frame_from_inputs(..., bridge=window_bridge)` (train and test rows of that window), and `expanding_window_scores(frame, Y, Y, ...)` for that single year; the per-year rows are aggregated with `summarise_scores`.
- Labels, rows, test years and both climatology baselines are identical on the two sides (the baselines never see the bridge), so each delta is paired and is due to the bridge alone. `skill = 1 − CRPS_EMOS / CRPS_trend-clim`.
- Timing caveats (both small, opposite signs): the window bridge is frozen for the whole test year, whereas a real-time refit could also use the year's earlier months (conservative); like the EMOS window (`climate/emos.py`), it ignores NOAA's ~2-week publication lag at the year boundary (slightly optimistic).
- The gate (`climate/paper_criteria.py`) and `climate/fit.py` are untouched; this study is additive. JSON sidecar carries the per-year scores and every window's bridge parameters.
