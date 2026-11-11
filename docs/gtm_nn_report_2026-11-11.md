# GTM research report -- neural post-processing and a nowcast against the operational EMOS

Runs of 2026-11-04 (seasonal), 2026-11-09 (nowcast) and 2026-11-11 (both again, on
completed inputs) on the research box, at commits 38b79ce and 6a551c2; the two differ
in the fetch script's default, its test and a runbook paragraph, not in a model. Code:
`climate/nn/` (features, strict windows, the two networks, baselines, scoring,
benchmarks, availability rules) and `scripts/research/` (`gtm_nn_benchmark.py`,
`gtm_nowcast_benchmark.py`, `gtm_nn_compare.py`, `gtm_nn_price.py`,
`gtm_nowcast.py`, `gtm_nn_fetch.py`). Run directories stay outside the repo and are
named here without their path. Research only: nothing here is wired into the paper
loop and nothing places orders.

**Verdict.** Neither network wins. The seasonal network is at or below EMOS at 1 of 6
leads, and it is under-dispersed and warm. The nowcast network is ahead of the bridge
baseline on the 10th of the month only; from the 15th it is behind, by a margin that
grows as the month fills, with the same warm, narrow bias. The operational EMOS stays
the pricing model. The result worth keeping from the nowcast work is its baseline, the
bridge of the month-to-date ERA5 mean (sections 6 and 8).

## 1. Data provenance

| Input | Source | Coverage | Rows |
|---|---|---|---|
| Label: NOAA NCEI monthly global land+ocean anomaly vs 1901-2000 (the GTM settling series) | `labels/labels_ledger.parquet`, series `gtm`; history is the current vintage, genuine first prints since the collector started on 2026-08-17 | 1850-01 .. 2026-09 (printed 2026-10-10) | 2,121 |
| Forecast: SEAS5 per-member monthly-mean 2 m temperature, cos-lat global mean | `forecasts/seas5_global_mean.parquet` | inits 1993-01 .. 2026-10, leads 1-6, complete only since 2026-11-11 (see below) | 79,308 (76,860 on 2026-11-04, 77,472 on 2026-11-09) |
| Reference: ERA5 monthly global-mean 2 m temperature (the bridge of the seasonal runs) | `reference/era5_global_mean.parquet` | 1993-01 .. 2025-12 | 396 |
| Daily: ERA5 6-hourly 2 m temperature on a 1 x 1 degree grid (181 x 360), reduced to daily cos-lat global means (the nowcast and its bridge) | `reference/era5_daily_global_mean.parquet`, fetched by `gtm_nn_fetch.py` one CDS request at a time | 1993-01-01 .. 2026-11-06, no day missing | 12,363 (12,053 through 2025) |

Availability rules (`climate/nn/availability.py`): an ERA5 day is public five days
after the day at 00:00 UTC; a SEAS5 init month is released on its 13th at 12:00 UTC;
a NOAA month prints on the 12th of the next month, except that a genuine first print in
the ledger counts from the time it was first seen, and two late prints are entered by
hand: December 2018 (2019-02-06, the shutdown) and September 2024 (2024-10-24,
Hurricane Helene). Each run asserts, test row by test row, that every input was public
by the row's decision time, and stops on the first violation.

**A hole in the seasonal store.** Until 2026-11-11 the store held no init month from
2026-01 to 2026-06: the bulk backfill ended with 2025, the monthly fetch began with
2026-07, and nothing in between was ever requested. `docs/gtm_model_report.md` gives
the coverage as 1993-01 .. 2026-08, but its 2,388 training rows are 398 init months, so
it was computed over the same hole. The first nowcast run exposed it (section 6). The
six months were fetched into the research root on 2026-11-11 with the operational
request builder, in one request: 1,836 rows, 51 members and 6 leads per init. Their
lead-1 ensemble mean sits 0.06 to 0.26 K below ERA5 in each month, inside or within
0.04 K of the 2017-2025 range for the same calendar month, so the new months are in
line with their neighbours. The live store still lacks them, and a refresh of the
research root from the mirror drops them again until the live store is completed.

## 2. What was built

- **Seasonal network v1** (`climate/nn/seasonal_model.py`, `torch_model.py`): one row
  per init x lead with 16 inputs (bridged ensemble mean and sd, the member quantiles
  10/25/50/75/90, the target year fraction, sin/cos of month of year, six lead
  indicators), standardised; two ReLU hidden layers of 32; outputs mu and sigma (softplus);
  loss the closed-form Gaussian CRPS; full-batch Adam, lr 1e-3, weight decay 1e-4, at
  most 500 epochs, early stopping with patience 50 on the last three target years of the
  training window; five seeds collapsed into one Gaussian (the mean of the means, the
  variance of the means added to the mean variance).
- **Nowcast network v1** (`nowcast_model.py`): the same machinery on eight inputs per
  target month and checkpoint: the bridged month-to-date mean, the fraction of days
  used, the previous month's label, the freshest seasonal mu and sigma for the month,
  sin/cos of month of year, the year fraction. Checkpoints at 00:00 UTC on the 5th,
  10th, 15th, 20th, 25th and 1, 3, 6, 10 days after month end, each cut at the ERA5
  lag: the 5th sees no day, +6 is the first that sees the whole month.
- **Baselines, the same code and splits:** trend climatology, the operational EMOS
  (trend variant, refit per window), the raw bridged ensemble. For the nowcast: the
  bridge of the month-to-date mean plus the climatological remainder of the month, its
  sigma from the spread of that remainder at the checkpoint and the bridge's residual;
  and persistence of the previous label.
- **Protocols.** The headline is the strict run: expanding windows by target year
  2001-2026, the bridge refit on each window's training years, a test row admitted
  only when it was decided on or after the publication of the last training label.
  The frozen run uses the model report's own frame (bridge fitted once, rows assigned
  by target year), runs only the baselines, and exists to show that they are the same
  code as `docs/gtm_model_report.md`; it is never the headline.
- **Win rules, as the benchmarks print them.** Seasonal: nn CRPS at or below emos at 4
  or more of 6 leads, with cover50 in [0.35, 0.65] and cover90 in [0.75, 0.98] at every
  lead. Nowcast: nn CRPS below the bridge's at every checkpoint in 10, 15, 20, 25, +1,
  +3 on the current-vintage table, and the first print table, wherever it has at least
  3 rows at a checkpoint, agrees in sign. Both rules compare the two models only on
  equal row counts; otherwise the run prints no verdict and says where the counts
  differ.

## 3. The frozen run reproduces the model report

`gtm_nn_benchmark.py --protocol frozen` on 2026-11-04 over test years 2001-2026, EMOS
column, against `docs/gtm_model_report.md` section 3 (labels through 2026-07 there,
through 2026-09 here; the store of that day held the inits through 2026-08):

| lead | n run / report | CRPS run / report | cover50 run / report | cover90 run / report |
|---:|---|---|---|---|
| 1 | 302 / 301 | 0.0402 / 0.0403 | 0.510 / 0.508 | 0.881 / 0.880 |
| 2 | 303 / 301 | 0.0486 / 0.0487 | 0.535 / 0.532 | 0.901 / 0.900 |
| 3 | 303 / 302 | 0.0501 / 0.0502 | 0.545 / 0.543 | 0.891 / 0.891 |
| 4 | 303 / 303 | 0.0516 / 0.0516 | 0.548 / 0.548 | 0.878 / 0.878 |
| 5 | 304 / 304 | 0.0543 / 0.0543 | 0.543 / 0.543 | 0.882 / 0.882 |
| 6 | 305 / 305 | 0.0579 / 0.0579 | 0.538 / 0.538 | 0.859 / 0.859 |

Equal to three decimals at every lead, exact at leads 4-6; the two newer labels add
one or two rows at leads 1-3. With `--last-test-year 2025` every lead has n = 300 and
EMOS CRPS 0.0403, 0.0486, 0.0502, 0.0517, 0.0546, 0.0584. The baselines are the
operational code.

## 4. Strict seasonal run: no win

`gtm_nn_benchmark.py --protocol strict --models nn,emos,raw,trend --gpu-budget 8`,
seeds 0-4, hidden 32, 26 windows (training rows 561 in 2001 to 2,361 in 2026), no
window skipped. Rows excluded by the strict rule: 396. That is 15 in each of the 26
years, the rows of the previous year's August to December inits whose targets fall in
the test year, all decided before the December label prints; and the six rows of the
2019-01 init, which the late December 2018 print pushes out.

Verdict, as printed by both runs: **no win (nn beats emos at 1 of 6 leads)**.

The table is the run on the completed store, `gtm_nn_strict_2026-11-11_filled`:

| model | lead | n | CRPS | skill vs trend | skill vs emos | cover50 | cover90 |
|---|---:|---:|---:|---:|---:|---:|---:|
| nn | 1 | 308 | 0.0483 | 0.385 | -0.184 | 0.354 | 0.721 |
| nn | 2 | 282 | 0.0489 | 0.354 | -0.076 | 0.408 | 0.809 |
| nn | 3 | 256 | 0.0493 | 0.301 | -0.132 | 0.391 | 0.754 |
| nn | 4 | 230 | 0.0479 | 0.293 | -0.097 | 0.422 | 0.722 |
| nn | 5 | 204 | 0.0500 | 0.254 | -0.043 | 0.412 | 0.789 |
| nn | 6 | 178 | 0.0512 | 0.251 | 0.046 | 0.438 | 0.820 |
| emos | 1 | 308 | 0.0407 | 0.481 | 0.000 | 0.519 | 0.870 |
| emos | 2 | 282 | 0.0455 | 0.400 | 0.000 | 0.543 | 0.918 |
| emos | 3 | 256 | 0.0436 | 0.383 | 0.000 | 0.590 | 0.918 |
| emos | 4 | 230 | 0.0437 | 0.355 | 0.000 | 0.539 | 0.909 |
| emos | 5 | 204 | 0.0479 | 0.285 | 0.000 | 0.554 | 0.897 |
| emos | 6 | 178 | 0.0536 | 0.215 | 0.000 | 0.522 | 0.882 |
| raw | 1 | 308 | 0.1171 | -0.492 | -1.874 | 0.062 | 0.169 |
| raw | 2 | 282 | 0.0930 | -0.228 | -1.046 | 0.167 | 0.486 |
| raw | 3 | 256 | 0.0895 | -0.268 | -1.054 | 0.152 | 0.480 |
| raw | 4 | 230 | 0.1014 | -0.496 | -1.321 | 0.130 | 0.417 |
| raw | 5 | 204 | 0.1212 | -0.808 | -1.528 | 0.108 | 0.348 |
| raw | 6 | 178 | 0.1453 | -1.126 | -1.710 | 0.039 | 0.264 |
| trend | 1 | 308 | 0.0785 | 0.000 | -0.927 | 0.497 | 0.860 |
| trend | 2 | 282 | 0.0758 | 0.000 | -0.666 | 0.504 | 0.862 |
| trend | 3 | 256 | 0.0706 | 0.000 | -0.620 | 0.508 | 0.859 |
| trend | 4 | 230 | 0.0678 | 0.000 | -0.551 | 0.526 | 0.861 |
| trend | 5 | 204 | 0.0670 | 0.000 | -0.398 | 0.529 | 0.868 |
| trend | 6 | 178 | 0.0684 | 0.000 | -0.275 | 0.539 | 0.860 |

n falls with the lead because the strict rule takes more at longer leads: nothing at
lead 1, and at lead L the first L - 1 targets of every test year, whose inits lie in
the year before.

PIT histogram, all leads, 10 bins:

| model | 0.0-0.1 | 0.1-0.2 | 0.2-0.3 | 0.3-0.4 | 0.4-0.5 | 0.5-0.6 | 0.6-0.7 | 0.7-0.8 | 0.8-0.9 | 0.9-1.0 |
|---|---|---|---|---|---|---|---|---|---|---|
| nn | 0.211 | 0.106 | 0.099 | 0.084 | 0.098 | 0.072 | 0.071 | 0.058 | 0.075 | 0.126 |
| emos | 0.060 | 0.072 | 0.091 | 0.103 | 0.126 | 0.108 | 0.106 | 0.106 | 0.101 | 0.128 |
| raw | 0.005 | 0.008 | 0.013 | 0.008 | 0.023 | 0.019 | 0.034 | 0.056 | 0.095 | 0.740 |
| trend | 0.095 | 0.087 | 0.071 | 0.100 | 0.112 | 0.098 | 0.107 | 0.086 | 0.097 | 0.147 |

What the table says: the network is worse than EMOS by 0.002 to 0.008 CRPS at leads
1-5 and better by 0.002 at lead 6; its cover90 is under the gate's floor of 0.75 at
leads 1 and 4, and its PIT puts 21 percent of the observations in the lowest decile,
twice the share of a calibrated forecast: the network runs too warm and too narrow.
EMOS, with four or five parameters per lead, sits inside both bands at every lead. The
network beats the raw ensemble and trend climatology everywhere, so it learns the
post-processing; it does not learn it better than EMOS on 561 to 2,361 rows.

The first run, `gtm_nn_strict_2026-11-04`, was made on the store of that day: inits
through 2026-08, none from 2026-01 to 2026-06. The completed store adds 4 to 7 scored
rows per lead, all in test year 2026, and leaves the verdict line as it was:

| run | nn CRPS, leads 1-6 | emos CRPS, leads 1-6 | verdict |
|---|---|---|---|
| `gtm_nn_strict_2026-11-04` | 0.0475, 0.0492, 0.0499, 0.0483, 0.0501, 0.0509 | 0.0407, 0.0458, 0.0439, 0.0438, 0.0476, 0.0527 | no win (nn beats emos at 1 of 6 leads) |
| `gtm_nn_strict_2026-11-11_filled` | 0.0483, 0.0489, 0.0493, 0.0479, 0.0500, 0.0512 | 0.0407, 0.0455, 0.0436, 0.0437, 0.0479, 0.0536 | no win (nn beats emos at 1 of 6 leads) |

## 5. Pricing the live ladders with both models

`gtm_nn_price.py` as of 2026-11-04T07:11Z with the operational `gtm_emos.json` and the
strict run's saved network (trained on labels through 2025-12), SEAS5 init 2026-10,
strikes from the recorder's contract cache of 2026-10-25:

| target | lead | EMOS mu, sigma | nn mu, sigma |
|---|---:|---|---|
| 2026-10 | 1 | 1.4238, 0.0556 | 1.6319, 0.0455 |
| 2026-11 | 2 | 1.4494, 0.0951 | 1.6762, 0.0554 |
| 2026-12 | 3 | 1.4251, 0.1149 | 1.6619, 0.0332 |

| strike | 2026-11 EMOS / nn | 2026-12 EMOS / nn |
|---:|---|---|
| 1.1 | 0.9999 / 1.0000 | 0.9977 / 1.0000 |
| 1.2 | 0.9956 / 1.0000 | 0.9750 / 1.0000 |
| 1.3 | 0.9419 / 1.0000 | 0.8620 / 1.0000 |
| 1.4 | 0.6983 / 1.0000 | 0.5866 / 1.0000 |
| 1.5 | 0.2972 / 0.9993 | 0.2573 / 1.0000 |

The October ladder (strikes 0.0 to 1.0) prices at 1.0000 under both models. At the
strikes that matter the network calls every outcome certain, 0.2 degC above EMOS with
half to a third of its sigma: the same warm, narrow bias as section 4, now on live
contracts. These prices are the reason the win rule has a calibration band. The
re-run of 2026-11-11 saved a network with the same weights (its last window trains on
labels through 2025-12, which the completed store does not touch), so the prices stand.

## 6. Strict nowcast run: no win

`gtm_nowcast_benchmark.py --models nn,bridge,persistence --gpu-budget 8`, seeds 0-4,
hidden 32, 26 windows (training rows 864 in 2001 to 3,564 in 2026), no window skipped.
A test year has 106 rows, 12 months at 9 checkpoints less two; 2019 has 99 and 2026 has
90, through the 10th of November.

Rows excluded by the strict rule: 59. That is January's 5th and 10th in each of the 26
years, decided before the December label prints on the 12th; and seven more in 2019,
where the December 2018 label printed on 2019-02-06: January 2019 from the 15th to +6
and February 2019's 5th. Test rows dropped for a missing ERA5 day: 7, the checkpoints
of November 2026 after the 10th, which lie beyond the series. No training row was
dropped: the daily series has no gap.

Verdict, as printed by the run on the completed store,
`gtm_nowcast_2026-11-11_filled`: **no win (nn does not beat bridge at 15)**.

| model | checkpoint | n | CRPS | skill vs bridge | cover50 | cover90 |
|---|---:|---:|---:|---:|---:|---:|
| nn | 5 | 0 | - | - | - | - |
| nn | 10 | 283 | 0.0527 | 0.116 | 0.322 | 0.675 |
| nn | 15 | 308 | 0.0494 | -0.034 | 0.312 | 0.685 |
| nn | 20 | 308 | 0.0433 | -0.144 | 0.364 | 0.731 |
| nn | 25 | 308 | 0.0401 | -0.345 | 0.377 | 0.763 |
| nn | +1 | 308 | 0.0388 | -0.695 | 0.328 | 0.773 |
| nn | +3 | 308 | 0.0392 | -0.821 | 0.347 | 0.776 |
| nn | +6 | 308 | 0.0402 | -0.941 | 0.357 | 0.776 |
| nn | +10 | 309 | 0.0401 | -0.929 | 0.359 | 0.777 |
| bridge | 5 | 0 | - | - | - | - |
| bridge | 10 | 283 | 0.0597 | 0.000 | 0.452 | 0.859 |
| bridge | 15 | 308 | 0.0477 | 0.000 | 0.487 | 0.857 |
| bridge | 20 | 308 | 0.0379 | 0.000 | 0.481 | 0.877 |
| bridge | 25 | 308 | 0.0298 | 0.000 | 0.487 | 0.867 |
| bridge | +1 | 308 | 0.0229 | 0.000 | 0.477 | 0.841 |
| bridge | +3 | 308 | 0.0215 | 0.000 | 0.468 | 0.864 |
| bridge | +6 | 308 | 0.0207 | 0.000 | 0.451 | 0.867 |
| bridge | +10 | 309 | 0.0208 | 0.000 | 0.450 | 0.864 |
| persistence | 5 | 282 | 0.0642 | - | 0.493 | 0.872 |
| persistence | 10 | 283 | 0.0641 | -0.073 | 0.495 | 0.873 |
| persistence | 15 | 308 | 0.0546 | -0.144 | 0.571 | 0.912 |
| persistence | 20 | 308 | 0.0546 | -0.441 | 0.571 | 0.912 |
| persistence | 25 | 308 | 0.0547 | -0.834 | 0.571 | 0.912 |
| persistence | +1 | 308 | 0.0547 | -1.390 | 0.571 | 0.912 |
| persistence | +3 | 308 | 0.0547 | -1.539 | 0.571 | 0.912 |
| persistence | +6 | 308 | 0.0547 | -1.638 | 0.571 | 0.912 |
| persistence | +10 | 309 | 0.0546 | -1.622 | 0.573 | 0.913 |

The 5th sees no ERA5 day, so the bridge and the network have no prediction there.
Persistence is a reference only: at the 5th and the 10th its label is two months old
while its sigma comes from one-month changes.

PIT histogram, all checkpoints, 10 bins:

| model | 0.0-0.1 | 0.1-0.2 | 0.2-0.3 | 0.3-0.4 | 0.4-0.5 | 0.5-0.6 | 0.6-0.7 | 0.7-0.8 | 0.8-0.9 | 0.9-1.0 |
|---|---|---|---|---|---|---|---|---|---|---|
| nn | 0.292 | 0.144 | 0.106 | 0.082 | 0.080 | 0.063 | 0.048 | 0.057 | 0.060 | 0.068 |
| bridge | 0.087 | 0.075 | 0.084 | 0.087 | 0.103 | 0.100 | 0.095 | 0.100 | 0.113 | 0.155 |
| persistence | 0.077 | 0.096 | 0.079 | 0.116 | 0.070 | 0.137 | 0.115 | 0.130 | 0.086 | 0.093 |

Genuine first prints, scored against the print itself, are two months (2026-08 and
2026-09), under the three rows the rule reads, so the table does not bind. It points
the same way:

| checkpoint | 10 | 15 | 20 | 25 | +1 | +3 | +6 | +10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| nn CRPS (n = 2) | 0.0684 | 0.0674 | 0.0512 | 0.0389 | 0.0360 | 0.0369 | 0.0364 | 0.0364 |
| bridge CRPS (n = 2) | 0.0641 | 0.0296 | 0.0185 | 0.0130 | 0.0154 | 0.0172 | 0.0177 | 0.0177 |

What the tables say. On the 10th, with five days of the month in, the network is ahead
of the bridge (0.0527 against 0.0597): the seasonal forecast and the previous label
carry information that five days do not. From the 15th the bridge is ahead and the gap
widens at every checkpoint. The bridge's CRPS falls from 0.048 to 0.021 as the month
fills; the network's stays near 0.040 from the 25th on. With the whole month observed
(+6) the bridge scores 0.0207 and the network 0.0402: the network does not learn to
trust a complete month-to-date mean. Its cover90 runs from 0.675 to 0.777 and its
cover50 from 0.312 to 0.377, and 29 percent of the observations fall in its lowest PIT
decile: too warm and too narrow again. The bridge is close to calibrated (cover50 0.45
to 0.49, cover90 0.84 to 0.88).

**The first run printed no verdict, and why.** `gtm_nowcast_2026-11-09` printed
`Verdict: none (unpaired rows at 10: nn has 281, bridge 283)`. The network had no
prediction on nine labelled rows where the bridge had one: June 2026 at every
checkpoint from the 10th, and July 2026 at the 10th. Their seasonal input was missing.
With no init month from 2026-01 to 2026-06 in the store (section 1), no forecast for
June existed at any time, and none for July before its own init was released on the
13th. The hole had a quieter effect too: January to May 2026 were scored with the
forecast of an init four to six months old, 39 rows in all.

On the completed store the nine rows are scored and the 39 use the fresh init. Every
row before 2026 and every bridge row is identical in the two runs, and so is the saved
network. The change is confined to 2026:

| run | 10th nn / bridge | 25th nn / bridge | +3 nn / bridge | verdict |
|---|---|---|---|---|
| `gtm_nowcast_2026-11-09` | 0.0529 / 0.0597 | 0.0396 / 0.0298 | 0.0390 / 0.0215 | none (unpaired rows at 10: nn has 281, bridge 283) |
| `gtm_nowcast_2026-11-11_filled` | 0.0527 / 0.0597 | 0.0401 / 0.0298 | 0.0392 / 0.0215 | no win (nn does not beat bridge at 15) |

The fresh input did not help the network on those five months. On the 15th its CRPS
over January to May 2026 is 0.062 with the fresh init and was 0.020 with the stale one;
the bridge has 0.042. Five months carry no conclusion, and the cause was not isolated.

## 7. Nowcast payloads

`gtm_nowcast.py` with the saved network of the nowcast run (trained on labels through
2025-12). A payload is usable when no public day is missing, the checkpoint is past the
5th, and the seasonal init is the latest one released.

- **Agreement with the benchmark.** As of 2026-09-25T00:00Z for September the payload
  gives mu 1.4159, sigma 0.0448 (20 days used, last ERA5 day 2026-09-20, init 2026-09,
  previous label 2026-08), and it matches the run's own row to 5e-8 in mu and 6e-9 in
  sigma. September then printed 1.33: the network was 0.09 too warm, 1.9 of its sigmas;
  the bridge on the same row says 1.3427 with sigma 0.0510.
- **October**, as of 2026-11-11T19:06Z, checkpoint +10, all 31 days:

  | model | mu | sigma |
  |---|---:|---:|
  | nowcast network | 1.5150 | 0.0703 |
  | bridge, the run's row at +10 | 1.4173 | 0.0355 |
  | persistence, September's label | 1.33 | 0.1018 |
  | EMOS, lead 1, init 2026-10 (section 5) | 1.4238 | 0.0556 |
  | seasonal network, lead 1 (section 5) | 1.6319 | 0.0455 |

  October prints about 2026-11-12 and will be the third genuine first print.
- **November, the running month**, as of 2026-11-11T19:06Z, checkpoint 10: mu 1.6600,
  sigma 0.0605, usable (6 of 30 days, last ERA5 day 2026-11-06, init 2026-10 because
  the November init is released on the 13th, previous label 2026-09). The run's own row
  at the checkpoint, 2026-11-10T00:00Z with five days: network 1.6691 with sigma
  0.0661, bridge 1.6286 with sigma 0.1036. EMOS at lead 2 from init 2026-10 says 1.4494
  with sigma 0.0951 (section 5). Both nowcasts read the first days of November as far
  warmer than the seasonal forecast did; with five days the bridge's own sigma is 0.10.
- The first attempt at November, on 2026-11-09, refused as designed (exit 3): the 5th
  is never scored, and three public days were missing until the month was refreshed.

## 8. What this supports, and what it does not

- Supports: the research package runs end to end on the research box with a real CDS
  account, and the frozen protocol reproduces the operational numbers, so a future model
  is scored on the same ground as EMOS. The strict protocol is stricter than the model
  report's expanding window (it refits the bridge per window and drops rows decided
  before their training labels were public), and EMOS keeps its skill under it.
- Supports: the bridge of the month-to-date mean as the nowcast. A month's lead-1 EMOS
  forecast is decided on the 13th and scores 0.0407. The bridge scores 0.0477 on the
  15th, 0.0379 on the 20th, 0.0298 on the 25th and 0.021 once the month is complete, on
  nearly the same 308 months. From about the 20th the month-to-date mean says more
  about the month's print than the seasonal forecast does. The two numbers come from
  two tables, not from a paired test.
- Does not support: replacing EMOS with the v1 seasonal network, using the v1 nowcast
  network in place of the bridge, or any live use of either. The scores are hindcast on
  current-vintage labels; the genuine first-print record holds two months.
- Not done here: a second version of either network. The seasonal design allows a
  mixture or quantile head only when v1's calibration fails; it did, so v2 is open,
  with the previous-label lag feature declined for it. Combining the lead-1 EMOS with
  the bridge before the 20th is the plain next test for the nowcast; it is not built.
- Not done here: the six init months in the live store. That changes the operational
  inputs (the EMOS fit gains 33 labelled rows) and waits for the account holder.

## 9. Operations learnt on the way

- The CDS notice of 2026-10-22 asks for one request at a time and suspends flagged
  accounts; the same account feeds the paper loop, so `gtm_nn_fetch.py` sends one
  request at a time by default (commit 6a551c2).
- Queue time follows the load of the day, not the size of the request. From 2026-11-05
  to 2026-11-09 a one-year request (CDS cost 8,760 against a limit of 121,000) waited
  2.2 to 3.3 hours, the first one 5.7, and then ran 2 to 4 minutes; the 33 years and
  the months of 2026 took 3.9 days. Splitting a year into months did not help: on
  2026-11-06 a one-month request (cost 744) waited 2 hours 18 minutes without starting
  and the monthly route was dropped. On 2026-11-11 a six-day request was served in 25
  seconds.
- A long fetch builds its month requests when it starts: the November request of the
  four-day fetch asked for the one day that was public on 2026-11-06. The running month
  needs its own `--refresh` on the day of a payload.
- A verdict of `none (unpaired ...)` points at an input, not at a model: list the rows
  one model lacks before reading any score. Here it found six missing init months that
  no earlier check had noticed.
- The API-only `grid` key is accepted by the new CDS: the one-month pilot delivered a
  181 x 360 netCDF with four steps a day, asserted by a script before the full fetch.
- The ERA5 hourly dataset is under CC-BY-4.0 now; the licence is accepted once per
  account on the dataset page, and the key file is checked with the same client.
- Before a payload or a pricing run: refresh the daily series for the month, then copy
  the current SEAS5 and labels files from the mirror into the research root; a stale
  init makes the payload unusable (exit 3) by design.
