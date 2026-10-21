#!/usr/bin/env bash
# Pull the Spark's live root into the Zenbook's mirror: the copy that lives off the Spark.
#
# Runs INSIDE WSL Ubuntu on the Zenbook (rsync lives there; Git Bash has none):
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/pull_snapshot.sh [--include-raw] [--dry-run]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/pull_snapshot.sh" [...]
# The scheduled task \Spark\Spark Pull runs it at logon and every six hours
# (scripts/scheduling/run_spark_pull.ps1) and pings an external check when it succeeds.
#
# Two copies, both one way, Spark -> Zenbook:
#   1. spark:/srv/quant_data          -> ~/quant_data          (the mirror)
#   2. spark:/srv/quant_data_backups  -> ~/quant_data_backups  (the daily copies with their
#      manifests; scripts/spark/restore_check.py verifies one of these)
# Rules:
#   * Refuses (exit 3) unless the mirror carries the marker file .mirror-of-spark, so it can
#     never write into a live root.
#   * No --delete, on purpose: this is a backup. If the Spark's root were ever emptied, a
#     mirroring delete would empty this copy too, at the one moment it is needed. The cost:
#     day directories the Spark has rotated away stay here until removed by hand.
#   * Skipped, as in push_snapshot.sh: ibkr_ticks/state/, *.tmp, SQLite journals, and
#     climate_data/raw/ (7.6 GB, re-downloadable) unless --include-raw.
#   * ssh never prompts (BatchMode) and gives up after 20 s: away from the tailnet the pull
#     fails fast, no ping is sent, and the next run tries again.
#   * The paper book arrives as a plain file copy. A pull that lands inside the daily sync
#     can copy it torn: the restore check says so and the next pull replaces it. The copies
#     that are consistent by construction are the day directories of copy 2.
# Exit: 0 done, 2 usage, 3 no marker; any other status is rsync's.
set -euo pipefail

SRC="${SPARK_SRC:-spark:/srv/quant_data}"
DEST="${QUANT_DATA_MIRROR:-/mnt/c/Users/asus/quant_data}"
BACKUPS_SRC="${SPARK_BACKUPS_SRC:-spark:/srv/quant_data_backups}"
BACKUPS_DEST="${QUANT_DATA_BACKUPS_MIRROR:-/mnt/c/Users/asus/quant_data_backups}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
MARKER=".mirror-of-spark"

DRY_RUN=0
INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "pull_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$DEST/$MARKER" ]; then
  echo "pull_snapshot: $DEST does not carry $MARKER; refusing to write into a root that is not marked as a mirror" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm" --exclude "/$MARKER")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
RSH="$SSH -o BatchMode=yes -o ConnectTimeout=20"
PULL=(rsync -rlt --stats --blocking-io "${EXCLUDES[@]}" -e "$RSH" "$SRC/" "$DEST/")
PULL_BACKUPS=(rsync -rlt --stats --blocking-io --exclude "*.partial/" -e "$RSH" "$BACKUPS_SRC/" "$BACKUPS_DEST/")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${PULL[*]}"
  echo "DRY-RUN: ${PULL_BACKUPS[*]}"
  exit 0
fi

echo "== 1/2 pull: $SRC -> $DEST"
set +e
"${PULL[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-pull (rsync 24, a writer raced us); retrying once"
  "${PULL[@]}"
elif [ "$rc" -ne 0 ]; then
  echo "pull_snapshot: the pull failed (rsync exit $rc)" >&2
  exit "$rc"
fi

echo "== 2/2 pull the daily backups: $BACKUPS_SRC -> $BACKUPS_DEST"
mkdir -p "$BACKUPS_DEST"
"${PULL_BACKUPS[@]}"
echo "pull_snapshot: done $(date -u +%FT%TZ)"
