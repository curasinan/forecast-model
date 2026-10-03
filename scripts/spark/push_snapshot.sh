#!/usr/bin/env bash
# Push a frozen copy of the Zenbook's quant data to the Spark. One way, never back.
#
# Runs INSIDE WSL Ubuntu (rsync lives there; Git Bash has none). From the Zenbook:
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/push_snapshot.sh [--include-raw]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" [--include-raw]
#               (REPO_WSL from scripts/spark/env.sh starts with //mnt on purpose: Git Bash
#               rewrites a bare /mnt/... argument into a Windows path before wsl.exe sees it)
#
# Steps (design: docs/plans/2026-09-30-spark-research-split-design.md section 4):
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
# climate_data/raw/ (7.6 GB of CDS downloads, re-downloadable) is excluded unless
# --include-raw. Avoid 17:25-17:40 local: the paper-sync task writes the SQLite DB.
set -euo pipefail

SRC="${QUANT_DATA_SRC:-/mnt/c/Users/asus/quant_data}"
SNAP="${QUANT_DATA_SNAPSHOT:-/mnt/c/Users/asus/quant_data_snapshot}"
DEST="${SPARK_DEST:-spark:quant_data}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
REMOTE_REPO="${SPARK_REMOTE_REPO:-~/work/forecast-model}"
REMOTE_PY="${SPARK_REMOTE_PY:-.venv/bin/python}"

INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "push_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
# Evidence that lives inside the snapshot: protected from the freeze's --delete
# (rsync never deletes excluded paths on the receiver) and never pushed.
KEEP=(--exclude "/parity/" --exclude "/identity_spark.json")

freeze() {
  rsync -rlt --delete "${EXCLUDES[@]}" "${KEEP[@]}" "$SRC/" "$SNAP/"
}

echo "== 1/4 freeze: $SRC -> $SNAP"
mkdir -p "$SNAP"
set +e
freeze
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-freeze (rsync 24, a writer raced us); retrying once"
  freeze
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

echo "== 2/4 push: $SNAP -> $DEST"
rsync -rlt --stats --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "${EXCLUDES[@]}" "${KEEP[@]}" \
  -e "$SSH" "$SNAP/" "$DEST/"

echo "== 3/4 verify on the Spark"
TMP_ID="$SNAP/identity_spark.json.partial"
if "$SSH" spark "cd $REMOTE_REPO && $REMOTE_PY scripts/spark/verify_mirror.py --root ~/quant_data" > "$TMP_ID"; then
  mv "$TMP_ID" "$SNAP/identity_spark.json"
else
  rc=$?
  rm -f "$TMP_ID"
  echo "push_snapshot: the verify step on the Spark failed (ssh exit $rc; see its output above); the previous identity is untouched" >&2
  exit "$rc"
fi

echo "== 4/4 identity (saved to $SNAP/identity_spark.json)"
cat "$SNAP/identity_spark.json"
