# Scheduling

Polymarket collectors are **retired** (2026-08-23). Do not install the old
`collect_markets` / `collect_snapshots` / `weather_collect_cycle` jobs.
Python CLIs that would have hit Gamma or CLOB now exit 2 unless
`POLYMARKET_RUNTIME=1` is set for a one-off archival run.

## IBKR (the live path)

| Job | Command | Cadence |
|---|---|---|
| IBKR quote recorder | `python scripts/record_ibkr_ticks.py` | Task Scheduler `\IBKR\IBKR Recorder` |
| Recorder health | `python scripts/check_ibkr_recorder.py` | hourly `\IBKR\IBKR Recorder Health` |
| Climate labels | `python scripts/collect_climate_labels.py` | daily (register separately, elevated) |
| GTM EMOS fit | `python scripts/fit_gtm_emos.py --trend --live` | after hindcast/label updates; `--live` names the live root (without it or `--root` the fit exits 2) |
| GTM paper signals | `python scripts/run_gtm_paper.py` | research; no orders |

Register IBKR jobs from an elevated PowerShell:

```powershell
.\scripts\scheduling\register_ibkr_tasks.ps1
```

Remove them with `unregister_ibkr_tasks.ps1`.

## Spark pull (the copy of the live root that lives off the Spark)

From the cutover of the runtime to the Spark on, this machine's `~/quant_data`
is a mirror of the Spark's live root (`docs/spark_runbook.md` section 7.6).

| Job | Command | Cadence |
|---|---|---|
| Pull the live root and the daily backups | `run_spark_pull.ps1` (WSL, `scripts/spark/pull_snapshot.sh`) | Task Scheduler `\Spark\Spark Pull`, at logon and every six hours |

Register it from an elevated PowerShell, after setting the user-level
variable `HEARTBEAT_URL_OFFBOX` (the script's header says how):

```powershell
.\scripts\scheduling\register_spark_pull_task.ps1
```

The script registers the task enabled, and a second run replaces the
registration, so it enables a task that was disabled on purpose. After the
cutover, never run it without
`Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'` after it,
unless the pull is meant to run.

Log: `logs\spark_pull_task.log`. The pull refuses a `~/quant_data` that does
not carry the file `.mirror-of-spark`. Remove the task with
`Unregister-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull' -Confirm:$false`.

## Leftover Polymarket Task Scheduler jobs

If `\Polymarket\collect_markets` (etc.) is still registered, uninstall:

```powershell
.\scripts\scheduling\unregister_polymarket_tasks.ps1
```

`register_tasks_windows.ps1` no longer creates those jobs; running it
unregisters leftovers.

Linux crontab: the entries in `crontab.txt` are commented historical
reference. Delete any live `collect_markets` / `collect_snapshots` lines.

## Override (archival only)

```powershell
$env:POLYMARKET_RUNTIME='1'
python -m collectors.collect_markets
```
