# Scheduling the collectors

Two jobs need to run on a timer:

| Job | Command | Cadence |
|---|---|---|
| `collect_markets` | `python -m collectors.collect_markets` | Daily at 06:00 and 18:00 local time |
| `collect_snapshots` | `python -m collectors.collect_snapshots` | Every 15 minutes |
| `weather_collect_cycle` | `python scripts/run_weather_collect_cycle.py` | Every 30 minutes (discover + forecasts + obs) |

Both must be invoked with the repo root as the working directory
(`C:\Users\asus\OneDrive\Desktop\new_work`) so relative paths
(`data/polymarket.db`, `logs/`) resolve correctly.

## Overlap protection — already handled in the scripts

Each collector acquires a PID lock on startup:

- `logs/collect_markets.lock`
- `logs/collect_snapshots.lock`

If a previous invocation is still running when the scheduler fires the
next one, the new process logs `"Previous ... run still active (pid=N);
skipping this cycle"` at INFO level and exits 0. Stale locks (file left
behind by a crashed process) are reclaimed automatically.

This means you don't need the scheduler to do anything special for
overlap — but belt-and-suspenders settings are provided below anyway.

## Logging

No redirection is needed. Both scripts already append to their log files
(`logs/market_collector.log`, `logs/snapshot_collector.log`) via a
Python `FileHandler` opened in append mode, and simultaneously echo to
stdout. Task Scheduler / cron can discard stdout without losing
anything.

---

## Windows — Task Scheduler

Run `register_tasks_windows.ps1` from an elevated PowerShell in the repo
root. It creates two scheduled tasks under the task folder `Polymarket`:

- `Polymarket\collect_markets` — at 06:00 and 18:00 daily
- `Polymarket\collect_snapshots` — every 15 minutes, starting at
  the next quarter hour, running indefinitely

Both tasks are registered with `MultipleInstancesPolicy = IgnoreNew`, so
Task Scheduler itself will refuse to start a second instance if one is
already running. Combined with the in-script PID lock, overlap is
impossible.

### Remove the tasks later

```powershell
Unregister-ScheduledTask -TaskPath "\Polymarket\" -TaskName "collect_markets" -Confirm:$false
Unregister-ScheduledTask -TaskPath "\Polymarket\" -TaskName "collect_snapshots" -Confirm:$false
```

### Manually fire a task (for testing)

```powershell
Start-ScheduledTask -TaskPath "\Polymarket\" -TaskName "collect_snapshots"
```

### Inspect history

Task Scheduler UI → `Task Scheduler Library \ Polymarket \ <task>` →
History tab. Each fire is logged with exit code. Exit 0 is the success
*and* "skipped because overlap" state — check the log file to
distinguish.

---

## Linux / macOS — cron

Paste the contents of `crontab.txt` into your user crontab:

```bash
crontab -e
# then paste the entries from crontab.txt
```

The cron entries use `flock -n` as a second layer of overlap protection
(redundant with the in-script PID lock, but harmless). `-n` means "don't
block" — if the lock is held, cron-side `flock` gives up immediately
instead of queueing.

Adjust `REPO` and `PY` in the file header to point at your repo and
Python interpreter.

---

## Sanity check

After registering, tail the logs to watch runs come in:

```bash
# Windows PowerShell
Get-Content logs\snapshot_collector.log -Wait -Tail 20

# Linux / macOS
tail -f logs/snapshot_collector.log
```

You should see a "Run summary" line every 15 minutes for snapshots and
one every 12 hours for the market collector.
