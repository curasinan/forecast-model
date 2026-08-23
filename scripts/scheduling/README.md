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
| GTM EMOS fit | `python scripts/fit_gtm_emos.py` | after hindcast/label updates |
| GTM paper signals | `python scripts/run_gtm_paper.py` | research; no orders |

Register IBKR jobs from an elevated PowerShell:

```powershell
.\scripts\scheduling\register_ibkr_tasks.ps1
```

Remove them with `unregister_ibkr_tasks.ps1`.

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
