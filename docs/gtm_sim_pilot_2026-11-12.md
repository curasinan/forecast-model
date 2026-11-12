# GTM simulation pilot, 2026-11-12: model against market, and the December book

The first two simulations of the paper-trading plan, run on the only ladders
the repository holds: the parity payloads of 2026-09-28 22:08 UTC
(`docs/spark_parity_2026-10-03_spark.json`) and 2026-10-16 13:31 UTC
(`docs/spark_runtime_cutover_2026-10-20_spark.json`). The recorder's sweeps,
the signals history and the label ledger stay on the Spark, and the machine
that ran this pilot could not reach NOAA, so nothing below is scored against a
print. Code: `climate/sim/` and `scripts/research/gtm_sim_{score,book}.py`.

**Verdict.** The market prices December 2026 about 0.14 degC cooler than the
model, with a wider spread: N(1.297, 0.179) against the model's N(1.425, 0.115)
from the October init. A December book bought at the ask is worth a large
expected gain if the model is right and about nothing if the market is. With the
$15 month cap the worst case is the $15.18 paid; at $30 the market view gives the
book a negative expectation and the same 30% chance of losing all of it.
Whether the model or the market has the mean right is the open question, and
settled prints answer it.

## 1. Market-implied distributions (S1, no labels)

The YES mid at strike k is read as c x P(Y > k) with Y ~ N(mu, sigma), fitted by
least squares weighted by 1 / spread. The ceiling c is fitted when the ladder has
a plateau. A fit counts as identified when at least two quotes sit between 10%
and 90% of the ceiling; otherwise mu and sigma trade off and the row is shown,
never scored.

| snapshot (UTC) | target | lead | model mu, sd | market mu, sd | ceiling | identified | P(> pivot) model / market |
|---|---|---:|---|---|---:|---|---|
| 2026-09-28 22:08 | 2026-09 | 0 (running) | 1.359, 0.077 | 1.325, 0.142 | 0.966 | no (strikes 0.5 apart) | >1.5: 0.029 / 0.102 |
| 2026-09-28 22:08 | 2026-12 | 3 | 1.437, 0.136 | 1.475, 0.314 | 0.919 | no (1.2 to 1.4 quoted 28 cents wide) | >1.1: 0.993 / 0.881 |
| 2026-10-16 13:31 | 2026-12 | 2 | 1.437, 0.136 | **1.297, 0.179** | 0.928 | **yes** | >1.4: 0.592 / 0.274 |

The model column is the forecast in the payload, from the 2026-08 init. The
2026-10 init priced December at N(1.4251, 0.1149)
(`docs/gtm_nn_report_2026-11-11.md` section 5), so the gap held at about 0.13
degC after a newer forecast.

Three things the ladders show:

- **Near-certain strikes trade at a discount.** December YES contracts at
  strikes the print cannot miss (above -0.2 degC) sit at 0.92 to 0.94 two months
  out; September's sat at 0.965 about two weeks before its print. A delayed NOAA
  release delays the resolution and does not cancel it
  (`docs/climate_oracle_research.md`), so the discount is a price of tying money
  up or of thin quoting, not a probability. The fit takes it out as the ceiling.
  Read at face value, the plateau would claim a 7% chance that December prints
  below -0.2 degC.
- **Most monthly ladders say little.** October was quoted 0.01 / 0.99 at every
  strike on both dates; November had no quotes at all on 2026-10-16; September's
  strikes stood 0.5 degC apart. Only December carried a 0.1-spaced ladder with
  3-cent spreads. So far, a settled month with an identified market fit is rare,
  and S1's evidence will come slower than the plan's five prints by January.
- **The edge gate counts the discount as edge.** On 2026-10-16 every December
  strike from -0.2 to 1.1 was `tradeable`, with an edge of 0.065 to 0.148 against
  the mid. The month cap leaves them no room behind 1.2 to 1.4, so the paper
  loop does not buy them today.

## 2. The December book at settlement (S4a)

Each book's settlement distribution is exact: between neighbouring strikes
its value is constant, and a YES at k pays iff the print, rounded to 0.01, is
above k. The cost includes the $0.01 commission. The positions are what the
reprice of 2026-11-12 would buy at the 2026-10-16 asks: `cap15` under the
default $15 month cap, `cap30` with `--max-group 30`. `old_limits_if_filled`
values the three original orders as though they had filled at their limits,
which they did not. `deep_itm` is $10 of YES above 1.0 at its ask, the
discount trade.

| book | positions | cost | worth now at mid / bid | breakeven mu |
|---|---|---:|---|---:|
| cap15 | 22 >1.3 @ 0.44, 8 >1.2 @ 0.65 | 15.18 | 14.43 / 13.98 | 1.281 |
| cap30 | 22 >1.3 @ 0.44, 15 >1.2 @ 0.65, 32 >1.4 @ 0.31 | 30.04 | 28.32 / 27.28 | 1.309 |
| old_limits_if_filled | 29 >1.2 @ 0.34, 41 >1.3 @ 0.24, 71 >1.4 @ 0.14 | 31.05 | 56.79 / 54.67 | 1.226 |
| deep_itm | 11 >1.0 @ 0.90 | 10.01 | 9.73 / 9.57 | 1.159 |

Breakeven mu is the December mean at which the book's expected P&L is zero, at
the model's sigma.

| book | scenario | mu, sd | E[P&L] | E[ROI] | P(loss) | P(lose all) | 5% P&L |
|---|---|---|---:|---:|---:|---:|---:|
| cap15 | model, 2026-10 init | 1.425, 0.115 | +11.34 | +75% | 15% | 3% | -7.18 |
| cap15 | model, 2026-08 init | 1.437, 0.136 | +10.80 | +71% | 17% | 4% | -7.18 |
| cap15 | half model, half market | 1.361, 0.147 | +5.94 | +39% | 35% | 14% | -15.18 |
| cap15 | market | 1.297, 0.179 | +1.01 | +7% | 52% | 30% | -15.18 |
| cap15 | model, mu - 0.10 | 1.325, 0.115 | +4.16 | +27% | 43% | 15% | -15.18 |
| cap15 | model, sigma x 1.5 | 1.425, 0.172 | +8.67 | +57% | 24% | 10% | -15.18 |
| cap30 | model, 2026-10 init | 1.425, 0.115 | +21.51 | +72% | 15% | 3% | -15.04 |
| cap30 | half model, half market | 1.361, 0.147 | +9.32 | +31% | 35% | 14% | -30.04 |
| cap30 | market | 1.297, 0.179 | -0.22 | -1% | 52% | 30% | -30.04 |
| deep_itm | model, 2026-10 init | 1.425, 0.115 | +0.99 | +10% | 0% | 0% | +0.99 |
| deep_itm | market | 1.297, 0.179 | +0.42 | +4% | 5% | 5% | -10.01 |

The full table, with every blend and stress, is in the run directory the
command in section 4 writes.

What it says:

- **The book is a bet on the mean.** cap15 loses money on average only if
  December's mean is below 1.28 degC. The market's fitted mean is 1.297, so even
  on the market's own numbers the book is about break-even (+$1.01, or -$0.75
  when the market's mids are read without taking the ceiling out). On the
  model's numbers it gains $11 on $15.
- **The spread is paid at once.** Bought at the ask, cap15 is worth $13.98 at
  the bid the moment it fills: $1.20 of its $15.18 is the spread and the
  commission, the price of filling.
- **A wider model costs little; a cooler one costs a lot.** Sigma times 1.5
  keeps +57%; mu 0.10 lower falls to +27% with a 15% chance of losing everything.
- **The $30 cap doubles the stake for the same odds.** Under the market view
  cap30 has a slightly negative expectation and a 30% chance of losing $30,
  three tenths of the $100 bankroll on one print. The $15 cap is the one to keep
  until settled prints say whose mean is right.
- **What waiting at the mid would have earned, had it filled.** The original
  orders would be worth $55 at today's bids for $31. They did not fill because
  the market moved toward the model without trading through them. That is the
  case for paying the ask, and also why the old limits are no longer available.

## 3. Limits of this pilot

- Two snapshots and one target month with an identified market. No row is
  scored; the scenario table is conditional on whose distribution is right,
  not an estimate of what will happen.
- The model rows are the payloads' forecasts, which carry the 2026-01..06 hole
  of the live SEAS5 store noted in the 2026-11-11 report.
- The market scenario takes the ceiling out. If the discount were a real
  belief about tail outcomes, the market view of cap15 would drop from +7% to
  about -5% (its value at the mids against its cost).

## 4. Running it on the Spark

S1 over everything recorded, scored against the ledger. With no `--payload` it
reads `paper/gtm_signals_history.jsonl` and `paper/gtm_signals.json` under the
data root, and the labels from `labels/labels_ledger.parquet`:

```bash
python scripts/research/gtm_sim_score.py --root ~/quant_data/climate_data
```

S4a on the book as it fills, with the latest payload and today's forecast:

```bash
python scripts/research/gtm_sim_book.py --payload ~/quant_data/climate_data/paper/gtm_signals.json --target 2026-12 --book "actual=1.3:YES:22:0.44,1.2:YES:8:0.65"
```

Each writes a run directory under `~/runs/` with `config.json`,
`metrics.json`, `rows.parquet` and `report.md`. Run them against the research
mirror, never the live root.

## 5. Next

1. The backfill of the November and December ladders before November expires
   around 2026-12-10 (plan section 2). Of the months seen so far, December is
   the one ladder that pins the market's distribution down.
2. S1 on the Spark as above: September and October have printed, and the
   history may hold snapshots in which their ladders were informative.
3. S2, the quote replay through the executor on a simulated venue, needs the
   sweep panel; it also allows S1 at any decision time instead of only when a
   payload was written.
