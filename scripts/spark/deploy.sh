#!/usr/bin/env bash
# Deliver main to the Spark's runtime clone and restart the recorder between two sweeps.
#
# Runs in Git Bash on the Zenbook, from any checkout of this repo:
#   bash scripts/spark/deploy.sh [--no-restart] [--dry-run]
#
# Steps (design: docs/plans/2026-10-05-spark-runtime-move-design.md section 5):
#   1. git push spark-rt main. The runtime clone (/home/trading/forecast-model) has
#      receive.denyCurrentBranch=updateInstead, so the push updates its checked-out main in
#      place; it is refused while a tracked file there has uncommitted changes.
#   2. Compare every unit file of the pushed commit with the installed one under
#      /etc/systemd/system. If one differs or is missing, print the sudo commands that
#      install it and stop with exit 3: installing a unit is the account holder's step.
#      Run the deploy again afterwards.
#   3. Unless --no-restart, and only if the recorder is running: wait until it has just
#      finished a sweep (its heartbeat file is younger than 120 s; at most 22 minutes, one
#      sweep interval plus two), then restart ibkr-recorder.service, so a deploy does not
#      cost a sweep. A recorder that is not running is left alone: a deploy never starts
#      the runtime (before the cutover, and after a rollback, it is stopped on purpose).
#      The timers' services are one-shot and pick the new code up at their next run.
#   4. Print the deployed commit and the units' states.
# Not done here: Python dependencies. When pyproject.toml or the constraints file changed,
# rebuild the runtime clone's venv first (docs/spark_runbook.md section 7.4). The sudoers
# drop-in and the Gateway's compose file are not compared either: a change to one of them
# is installed by hand in the same sitting.
# Exit: 0 done, 2 usage, 3 a unit file must be installed by hand first; any other status is
# the failing step's own.
set -euo pipefail

# Git Bash rewrites arguments that look like POSIX paths before a Windows program (ssh.exe)
# sees them; the remote commands below are full of such paths.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

DRY_RUN=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "deploy: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "${SSH:-}" ]; then
  if [ -x /c/Windows/System32/OpenSSH/ssh.exe ]; then
    SSH=/c/Windows/System32/OpenSSH/ssh.exe   # the client the repo's core.sshCommand names
  else
    SSH=ssh
  fi
fi
GIT="${DEPLOY_GIT:-git}"
REMOTE="${DEPLOY_REMOTE:-spark-rt}"       # the git remote and the ssh host of the trading user
OPERATOR="${DEPLOY_OPERATOR:-spark}"      # the ssh host of the operator, who may restart units
CLONE=/home/trading/forecast-model
HEARTBEAT=/srv/quant_data/ibkr_ticks/state/recorder_state.json
UNITS="ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer"

REPO_ROOT="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd)"
cd "$REPO_ROOT"

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

# Each of these is one remote command line, run by the Spark's shell.
DIFF_CMD='cd '"$CLONE"' && for f in deploy/spark/systemd/*; do [ -e "$f" ] || continue; n=$(basename "$f"); cmp -s "$f" "/etc/systemd/system/$n" || echo "$n"; done'
STATE_CMD='systemctl is-active ibkr-recorder.service'
WAIT_CMD='f='"$HEARTBEAT"'; i=0; while [ "$i" -lt 132 ]; do age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) )); if [ "$age" -lt 120 ]; then echo "heartbeat is ${age}s old"; exit 0; fi; i=$((i + 1)); if [ $((i % 6)) -eq 0 ]; then echo "waiting for the sweep to finish (heartbeat ${age}s old)"; fi; sleep 10; done; exit 1'
RESTART_CMD='sudo -n /usr/bin/systemctl restart ibkr-recorder.service'
STATUS_CMD='git -C '"$CLONE"' log --oneline -1; for u in '"$UNITS"'; do echo "$u: $(systemctl is-active "$u")"; done'

echo "== 1/4 push main to $REMOTE"
run "$GIT" push "$REMOTE" main

echo "== 2/4 unit files: the pushed commit against /etc/systemd/system"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH $REMOTE $DIFF_CMD"
  changed=""
else
  changed=$("$SSH" "$REMOTE" "$DIFF_CMD" | tr -d '\r')
fi
if [ -n "$changed" ]; then
  echo "deploy: these unit files differ from the installed ones, or are not installed:"
  for n in $changed; do echo "  $n"; done
  echo "The account holder installs them on the Spark, then this deploy is run again:"
  for n in $changed; do
    echo "  sudo install -m 644 -o root -g root $CLONE/deploy/spark/systemd/$n /etc/systemd/system/$n"
  done
  echo "  sudo systemctl daemon-reload"
  exit 3
fi

if [ "$RESTART" -eq 0 ]; then
  echo "== 3/4 skipped (--no-restart): the recorder keeps running the code it started with"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "== 3/4 restart the recorder right after a sweep, if it is running"
  echo "DRY-RUN: $SSH $REMOTE $STATE_CMD"
  echo "DRY-RUN: $SSH $REMOTE $WAIT_CMD"
  echo "DRY-RUN: $SSH $OPERATOR $RESTART_CMD"
else
  echo "== 3/4 restart the recorder right after a sweep, if it is running"
  state=$("$SSH" "$REMOTE" "$STATE_CMD" | tr -d '\r' || true)
  if [ "$state" != "active" ]; then
    echo "deploy: the recorder is not running on the Spark (state: ${state:-unknown}); a deploy does not start it"
  else
    set +e
    "$SSH" -o ServerAliveInterval=30 "$REMOTE" "$WAIT_CMD"
    rc=$?
    set -e
    case "$rc" in
      0) echo "deploy: a sweep has just finished" ;;
      1) echo "deploy: no sweep finished within 22 minutes; the recorder is not completing sweeps, so a restart costs none" ;;
      *) echo "deploy: could not watch the heartbeat (ssh exit $rc); the recorder was NOT restarted" >&2
         exit "$rc" ;;
    esac
    "$SSH" "$OPERATOR" "$RESTART_CMD"
  fi
fi

echo "== 4/4 deployed commit and unit states"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH $REMOTE $STATUS_CMD"
else
  "$SSH" "$REMOTE" "$STATUS_CMD" || echo "deploy: could not read the units' states" >&2
fi
