# Cutover of the runtime to the Spark, 2026-10-20

Since 2026-10-20 09:10 UTC the DGX Spark runs the IB Gateway, the ForecastEx
quote recorder, the paper sync, the climate-label ledger and the paper-book
backup; the Zenbook's four scheduled tasks are disabled and its old live root
is kept as `~/quant_data_zenbook_final` for one rollback week. Every block
below is pasted from the command that produced it; the paper account is called
"the paper account", no id, no address, no token.

## When

| What | When (Europe/Istanbul) |
| --- | --- |
| Evening start (preconditions, sync, freeze) | 2026-10-19 22:33 |
| Zenbook frozen (tasks disabled, Gateway logged out) | 2026-10-19 22:38 |
| Paused: the Spark's container start and the root swap had not happened | 2026-10-19 22:55 to 2026-10-20 11:45 |
| Container up on the Spark, logged out | 2026-10-20 11:45 |
| Fresh sync on the Zenbook, logout again, push, parity, swap | 2026-10-20 11:50 to 11:54 |
| Login on the Spark; `SINCE` | 2026-10-20 11:57 (`2026-10-20T08:57:35+00:00`) |
| Recorder started on the Spark | 11:58 |
| First sweep on the Spark | 12:10 (`quotes-091002Z.parquet`, 2,426 rows) |
| Timers enabled, sync and probe exercised, test alert | 12:12 |
| Gate | 12:13, `CUTOVER GATE: PASS` |

Recording was interrupted from the Zenbook's last complete sweep,
`2026-10-16 13:31 UTC` (its Gateway had been closed since that afternoon; the
sweep that ran at the freeze on 2026-10-19 was cut), to the Spark's first,
`2026-10-20 09:10 UTC`: three days and twenty hours, owed to the hourly
backfill of the first week.

The evening had been planned for 20:00 and started at 22:33; the Gateway's
daily restart was therefore moved from 23:45 to 03:30 Europe/Istanbul. IBC
wants the hour with two digits (`03:30 AM`); the first spelling (`3:30 AM`)
was refused at the login and the Gateway kept the time stored during the
spike, so the first daily restart ran at 22:10 on 2026-10-20 (the session
survived it). The operator's `.env` now says `03:30 AM`; it takes effect at
the next container start.

## Commit

Both machines ran `b9eed400654116c42193842e06263f3f9b00990c` (`main` on the
Zenbook, the runtime clone on the Spark, `0` changed files; the Spark's suite
`1999 passed, 12 skipped`).

## The Zenbook let go, and the Spark had nothing (steps 3 and 8)

Before the Spark's container was started, and again right before the login
on the Spark:

```
session check: no Gateway here (port 4002 closed, no ibgateway process)
zenbook: exit=0
```

with `0` Python scripts running and the four tasks `Disabled`. The Spark's
container came up at the login dialog (`LoginState is LOGGED_OUT`), with
`127.0.0.1:4002` and `127.0.0.1:5900` the only listening sockets and no
restart token on disk. One thing seen while freezing: with the Zenbook's
tasks running under `conhost --headless` (change of 2026-10-19 00:52),
`Stop-ScheduledTask` ended the wrapper and left the recorder's Python process
running; it was stopped by hand.

## Both roots held the same operational files (step 5)

The last push ran with the Zenbook frozen, after a sync made minutes before
(`examined=3 filled=0 partially_filled=0 cancelled=0 unchanged=3
unmatched_venue_orders=0`):

```
9 operational files; differing: []
```

The two manifests are kept outside the repository, in
`~/quant_data_snapshot/parity/cutover_2026-10-19/` on the Zenbook
(`manifest_zenbook.json`, `manifest_spark.json`), with `since.txt` and
`gate.txt`.

## Parity (step 6)

```
ladder=38 tradeable=17 fingerprint=2fd53f921150c094 sweep=2026-10-16T13:31:46Z
numeric leaves compared: 1488; max abs diff 4.219e-12; max rel diff 1.491e-11
machines: left AMD64/Windows/py3.14.2 vs right aarch64/Linux/py3.14.2
PARITY OK
```

The two payloads are beside this report:
`docs/spark_runtime_cutover_2026-10-20_zenbook.json` and
`docs/spark_runtime_cutover_2026-10-20_spark.json`.

## The gate (step 10)

`SINCE = 2026-10-20T08:57:35+00:00`. `gate.txt`, verbatim:

```
PASS sweep: quotes-091002Z.parquet, 2426 rows
PASS health: probe exit 0 at 09:12:38Z, test alert delivered by telegram
PASS sync: book carried over (examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled) (before 2026-10-20T08:50:41Z, after 2026-10-20T09:10:41Z, 0 not-ok runs between)
PASS parity: compare_json exit 0
    numeric leaves compared: 1488; max abs diff 4.219e-12; max rel diff 1.491e-11
    machines: left AMD64/Windows/py3.14.2 vs right aarch64/Linux/py3.14.2
    PARITY OK
CUTOVER GATE: PASS
```

One thing learned on the way to the health line: a probe run while
`ibkr-health.timer` is still disabled leaves no `ExecMain*` record in
`systemctl show`, because nothing references the finished one-shot unit and
systemd unloads it. With the timer enabled the record stays
(`ExecMainExitTimestamp=@1792487558 ExecMainStatus=0`), which is what the
gate reads.

## After the gate (step 12)

The Spark's research mirror was marked and refreshed:

```
mirror_live: ~/quant_data refreshed from /srv/quant_data
```

and the marker now guards both mirrors from both sides. On the Zenbook's new
mirror a live-writing script refuses (`refusing to write GTM paper signals:
... is a mirror of the live root`, exit 2) and a push from it refuses
(`push_snapshot: ... carries .mirror-of-spark; it is a mirror of the Spark's
live root and is never pushed`, exit 3). The pull task `\Spark\Spark Pull`
was enabled and run once by the account holder and failed: rsync through the
Windows ssh client under WSL reported `write failed 11: Resource temporarily
unavailable`, `Invalid block index` and `error in socket IO (code 10)` within
seconds, by hand as well as from the task, on 13 MB of data. rsync's
non-blocking I/O does not survive that transport; with `--blocking-io` the
same pull completed (1,152 files). The flag is now on every rsync that
crosses ssh (`pull_snapshot.sh`, `rollback.sh`, whose copy runs in the same
direction, and `push_snapshot.sh`); the task's next run is the proof.

## Done by hand (the account holder)

Opened the Zenbook's Gateway and logged in for the fresh sync, logged out and
closed it; disabled the four tasks and stopped the recorder task; logged in
on the Spark over the VNC tunnel; `systemctl enable --now` for the recorder
and then for the four timers; confirmed the test alert on the phone; enabled
and started the pull task. The container start, the root swap, the sync by
hand, the push, the parity, the marker and the gate were run from the
Zenbook by the executor.

## Owed to the first week

- The hourly backfill for 2026-10-16 13:31 UTC to 2026-10-20 09:10 UTC
  (Task 13 step 2 of the plan), from the Spark's runtime clone.
- The Zenbook task wrapper's stop path (the orphaned recorder process), only
  relevant for the rollback week.
- `ple-warm.service` on the Spark, left over from the previous LLM, can be
  disabled.
- On its first morning (2026-10-21, 10:16) the Gateway lost its server
  connection and sat at `Connecting to server...` for hours while the API
  stayed up, so the recorder's sweeps returned no rows and the probe said
  DEGRADED every hour; a login over VNC at 12:55 brought it back without a
  container restart. The first week watches whether this repeats.
