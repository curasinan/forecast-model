#!/usr/bin/env bash
# Push a frozen copy of the Zenbook's quant data to the Spark.
#
# Runs INSIDE WSL Ubuntu (rsync lives there; Git Bash has none). From the Zenbook:
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/push_snapshot.sh [--include-raw] [--cutover] [--dry-run]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" [...]
#               (REPO_WSL from scripts/spark/env.sh starts with //mnt on purpose: Git Bash
#               rewrites a bare /mnt/... argument into a Windows path before wsl.exe sees it)
#
# Until the cutover the
# Zenbook's ~/quant_data is the live root and this script feeds the Spark's research mirror.
# From the cutover on, the Zenbook's root is itself a mirror and carries the marker file
# .mirror-of-spark; this script then REFUSES to run (exit 3), because a mirror is never
# pushed over anything. Data flows from the Spark from then on (pull_snapshot.sh).
#
# Steps:
#   1. freeze  ~/quant_data -> ~/quant_data_snapshot   (local, --delete, so the Zenbook-side
#      parity run reads exactly the bytes the Spark gets). parity/ and identity_spark.json
#      inside the snapshot are evidence and survive the --delete. The recorder's state/ dir,
#      *.tmp files and SQLite journals are skipped: they change under our feet, and a file
#      that vanishes mid-copy makes rsync exit 24, which is retried once.
#   2. push    ~/quant_data_snapshot -> spark:quant_data (no --delete: Spark-side
#      refits/experiments may coexist; canonical files are overwritten).
#   3. verify  on the Spark: scripts/spark/verify_mirror.py (exit 1 lists every unreadable
#      file; a push that lands inside the recorder's sub-second parquet write copies a
#      torn file -- re-run the push). The identity goes to a temp file and is moved into
#      place only on success, so a failed verify never leaves an empty identity behind.
#   4. print   the Spark's snapshot identity.
#
# --cutover is for the cutover evening and for seeding before it: the push goes into the
# Spark's live root (/srv/quant_data, as the trading user through the ssh host spark-rt)
# instead of the research mirror, and the verify step runs there from the runtime clone.
# --dry-run prints the three commands and runs nothing.
# climate_data/raw/ (7.6 GB of CDS downloads, re-downloadable) is excluded unless
# --include-raw. Avoid 17:25-17:40 local: the paper-sync task writes the SQLite DB.
set -euo pipefail

SRC="${QUANT_DATA_SRC:-/mnt/c/Users/asus/quant_data}"
SNAP="${QUANT_DATA_SNAPSHOT:-/mnt/c/Users/asus/quant_data_snapshot}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
HOST="${SPARK_HOST:-spark}"
DEST="${SPARK_DEST:-$HOST:quant_data}"
REMOTE_ROOT="${SPARK_REMOTE_ROOT:-~/quant_data}"
REMOTE_REPO="${SPARK_REMOTE_REPO:-~/work/forecast-model}"
REMOTE_PY="${SPARK_REMOTE_PY:-.venv/bin/python}"
MARKER=".mirror-of-spark"

INCLUDE_RAW=0
CUTOVER=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --include-raw) INCLUDE_RAW=1 ;;
    --cutover) CUTOVER=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "push_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ "$CUTOVER" -eq 1 ]; then
  HOST=spark-rt
  DEST=spark-rt:/srv/quant_data
  REMOTE_ROOT=/srv/quant_data
  REMOTE_REPO=/home/trading/forecast-model
fi

if [ -e "$SRC/$MARKER" ]; then
  echo "push_snapshot: $SRC carries $MARKER; it is a mirror of the Spark's live root and is never pushed" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
KEEP=(--exclude "/parity/" --exclude "/identity_spark.json")

FREEZE=(rsync -rlt --delete "${EXCLUDES[@]}" "${KEEP[@]}" "$SRC/" "$SNAP/")
PUSH=(rsync -rlt --stats --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "${EXCLUDES[@]}" "${KEEP[@]}"
      -e "$SSH" "$SNAP/" "$DEST/")
VERIFY="cd $REMOTE_REPO && $REMOTE_PY scripts/spark/verify_mirror.py --root $REMOTE_ROOT"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${FREEZE[*]}"
  echo "DRY-RUN: ${PUSH[*]}"
  echo "DRY-RUN: $SSH $HOST $VERIFY"
  exit 0
fi

echo "== 1/4 freeze: $SRC -> $SNAP"
mkdir -p "$SNAP"
set +e
"${FREEZE[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-freeze (rsync 24, a writer raced us); retrying once"
  "${FREEZE[@]}"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

echo "== 2/4 push: $SNAP -> $DEST"
"${PUSH[@]}"

echo "== 3/4 verify on the Spark"
TMP_ID="$SNAP/identity_spark.json.partial"
if "$SSH" "$HOST" "$VERIFY" > "$TMP_ID"; then
  mv "$TMP_ID" "$SNAP/identity_spark.json"
else
  rc=$?
  rm -f "$TMP_ID"
  echo "push_snapshot: the verify step on the Spark failed (ssh exit $rc; see its output above); the previous identity is untouched" >&2
  exit "$rc"
fi

echo "== 4/4 identity (saved to $SNAP/identity_spark.json)"
cat "$SNAP/identity_spark.json"
