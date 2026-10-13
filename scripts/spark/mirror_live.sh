#!/usr/bin/env bash
# Refresh the research mirror from the live root. Runs ON THE SPARK, as the operator.
#
#   bash scripts/spark/mirror_live.sh [--include-raw] [--dry-run]
#
# After the cutover /srv/quant_data is the one live root, written by the trading user only.
# The operator's ~/quant_data is a copy of it for research and carries the marker file
# .mirror-of-spark.
#   * Refuses (exit 3) to write into a root WITHOUT the marker, so it can never be pointed
#     at a live root; and refuses a source WITH the marker (that is a mirror, not the live
#     root).
#   * No --delete: files that research wrote into the mirror stay; a file the live root
#     also holds is overwritten by the live one.
#   * Skipped, as in push_snapshot.sh: ibkr_ticks/state/, *.tmp, SQLite journals, and
#     climate_data/raw/ unless --include-raw.
# The live root is only read here, through the trading group. Avoid 17:25-17:40 local (the
# paper sync writes the SQLite book), and verify before scoring on the mirror:
#   .venv/bin/python scripts/spark/verify_mirror.py --root ~/quant_data
# Exit: 0 done, 2 usage, 3 refused; any other status is rsync's.
set -euo pipefail

SRC="${LIVE_ROOT:-/srv/quant_data}"
DEST="${MIRROR_ROOT:-$HOME/quant_data}"
MARKER=".mirror-of-spark"

DRY_RUN=0
INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "mirror_live: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$DEST/$MARKER" ]; then
  echo "mirror_live: $DEST does not carry $MARKER; refusing to write into a root that is not marked as a mirror" >&2
  exit 3
fi
if [ -e "$SRC/$MARKER" ]; then
  echo "mirror_live: the source $SRC carries $MARKER; it is a mirror, not the live root" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm" --exclude "/$MARKER")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
COPY=(rsync -rlt --stats "${EXCLUDES[@]}" "$SRC/" "$DEST/")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${COPY[*]}"
  exit 0
fi

set +e
"${COPY[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-copy (rsync 24, a writer raced us); retrying once"
  "${COPY[@]}"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
echo "mirror_live: $DEST refreshed from $SRC"
