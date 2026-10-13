#!/usr/bin/env bash
# Roll the runtime back from the Spark to the Zenbook: the mirror image of the cutover.
#
# Runs INSIDE WSL Ubuntu on the Zenbook (it needs rsync), like push_snapshot.sh:
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" [--no-copy] [--dry-run]
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/rollback.sh [--no-copy] [--dry-run]
#
# The Gateway session is on one machine at a time, so the ORDER below is the point: the
# Spark gives the session up and is checked before anything is copied, and the Zenbook logs
# in only after this script has ended with exit 0.
#   1. Stop the Spark's timers, then its services: one sudo-allowed systemctl call per unit.
#   2. Stop the Gateway container: cd ~/ibgw && docker compose stop.
#   3. Session check on the Spark: nothing listens on 4002 and the container does not run.
#      This machine's own copy of session_check.py is piped to the Spark's python3, so the
#      check does not depend on what any clone over there has checked out.
#   If step 1, 2 or 3 fails -- the Spark cannot be reached, sudo or docker refuse, the check
#   finds a Gateway -- the script stops with exit 4 and says so: nothing has been copied, and
#   nobody may log in on the Zenbook, because the Spark may still hold the session. A Spark
#   that cannot be reached cannot be proved to have let go from here: wait until it answers,
#   or stop the container at the machine itself. The same warning is printed when the script
#   is interrupted (Ctrl-C, a closed terminal, a kill) before the check has passed: the Spark
#   may be stopped half way. Every ssh call here runs with BatchMode and keepalive options
#   (SSH_OPTS), so a Spark that stops answering ends the call within about a minute instead
#   of hanging.
#   4. Copy the Spark's live root into the Zenbook's kept root, ~/quant_data_zenbook_final.
#      No --delete: nothing the Zenbook holds is removed. This is the ONLY place where data
#      is copied from the Spark into a Zenbook live root; it refuses (exit 3) a destination
#      that is missing or that carries the mirror marker, before the Spark has been asked
#      anything. The SQLite journal, WAL and SHM files go with their databases, unlike in the
#      other copy scripts: every writer is stopped by now, so a journal that exists belongs to
#      its database, and without it a stop that landed mid-transaction would leave the Zenbook
#      a half-written book with nothing to roll it back. If the copy fails (exit 5), the
#      session has already been released: run the script again to finish the copy before
#      anyone logs in.
#   5. Print what the account holder does next. None of it is done here.
# --no-copy ends after step 3: the Spark side only, for a rehearsal with everything stopped.
# Exit: 0 done, 2 usage, 3 destination refused (the Spark was not touched and may still hold
# the session; --no-copy releases it), 4 the Spark may still hold the session, 5 the copy
# failed after the session was released (rsync's own status is in the message).
set -euo pipefail

HOST="${SPARK_HOST:-spark}"
SRC="$HOST:/srv/quant_data"
DEST="${ROLLBACK_DEST:-/mnt/c/Users/asus/quant_data_zenbook_final}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
MARKER=".mirror-of-spark"
TIMERS="ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer"
SERVICES="ibkr-health.service ibkr-paper-sync.service climate-labels.service paper-book-backup.service ibkr-recorder.service"
HERE="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"

DRY_RUN=0
COPY=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-copy) COPY=0 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "rollback: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

refuse_destination() {
  echo "rollback: $1" >&2
  echo "rollback: the Spark was not touched and may still hold the session;" \
       "--no-copy releases it" >&2
  exit 3
}

if [ "$COPY" -eq 1 ]; then
  if [ ! -d "$DEST" ]; then
    refuse_destination "$DEST does not exist; the root kept at the cutover is the only destination"
  fi
  if [ -e "$DEST/$MARKER" ]; then
    refuse_destination "$DEST carries $MARKER; it is a mirror, not the kept live root"
  fi
fi

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

RELEASED=0
WARNED=0
warn_not_released() {
  WARNED=1
  echo "rollback: the Spark may still hold the Gateway session and nothing was copied. Do NOT log in on the Zenbook." >&2
}

still_held() {
  echo "rollback: $1" >&2
  warn_not_released
  exit 4
}

on_exit() {
  if [ "$RELEASED" -eq 0 ] && [ "$WARNED" -eq 0 ]; then
    echo "rollback: stopped before the session check passed" >&2
    warn_not_released
  fi
}

# Timers first, so nothing starts a service that has just been stopped.
STOP_CMD=""
for unit in $TIMERS $SERVICES; do
  STOP_CMD+="sudo -n /usr/bin/systemctl stop $unit && "
done
STOP_CMD+="echo 'all runtime units are stopped'"
CHECK_CMD="python3 - --port 4002 --container ibgw"

echo "== 1/5 stop the Spark's timers and services"
if [ "$DRY_RUN" -eq 0 ]; then
  trap on_exit EXIT
fi
run "$SSH" "${SSH_OPTS[@]}" "$HOST" "$STOP_CMD" || still_held "the Spark's units could not be stopped (exit $?)"

echo "== 2/5 stop the Gateway container"
run "$SSH" "${SSH_OPTS[@]}" "$HOST" "cd ~/ibgw && docker compose stop" \
  || still_held "the Gateway container could not be stopped (exit $?)"

echo "== 3/5 session check on the Spark"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH ${SSH_OPTS[*]} $HOST $CHECK_CMD < $HERE/session_check.py"
elif "$SSH" "${SSH_OPTS[@]}" "$HOST" "$CHECK_CMD" < "$HERE/session_check.py"; then
  RELEASED=1
else
  still_held "the session check on the Spark did not pass"
fi

if [ "$COPY" -eq 0 ]; then
  echo "== 4/5 and 5/5 skipped (--no-copy): the Spark has given the session up; nothing was copied"
  exit 0
fi

echo "== 4/5 copy $SRC -> $DEST (nothing is deleted there)"
EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "/$MARKER")
COPY_CMD=(rsync -rlt --stats "${EXCLUDES[@]}" -e "$SSH -o BatchMode=yes -o ConnectTimeout=20"
          "$SRC/" "$DEST/")
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${COPY_CMD[*]}"
else
  set +e
  "${COPY_CMD[@]}"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "rollback: copy failed (rsync exit $rc); run this script again to finish the copy" \
         "before anyone logs in on the Zenbook. The Spark has given the session up." >&2
    exit 5
  fi
fi

echo "== 5/5 by hand, in this order (the account holder)"
cat <<'EOF'
  On the Spark, so that a reboot does not start the runtime again:
    sudo systemctl disable ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer
  On the Zenbook, in an elevated PowerShell, from the repo root:
    Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
    python scripts/spark/restore_check.py --from ~/quant_data_zenbook_final --scratch <a fresh directory>
      (RESTORE OK must come first; a FAIL line names the file)
    .\scripts\scheduling\swap_live_root_for_mirror.ps1 -Undo
    Get-ScheduledTask -TaskPath '\IBKR\','\Climate\' | Where-Object TaskName -in 'IBKR Recorder','IBKR Recorder Health','IBKR Paper Sync','Climate Labels' | Enable-ScheduledTask
  Only after the swap has printed "live root:", open IB Gateway on the Zenbook and log in.
  The recorder task starts within five minutes.
EOF
