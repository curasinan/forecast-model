# Shared shell variables for the Spark work. From the repo root of any checkout, in Git Bash:
#   source scripts/spark/env.sh
# Shell state does not survive between Claude's Bash tool calls, hence one file.
SSH=/c/Windows/System32/OpenSSH/ssh.exe   # Windows OpenSSH: reads ~/.ssh/config (Host spark)
SCP=/c/Windows/System32/OpenSSH/scp.exe
# REPO_WSL is derived from this file's own location, so it names the checkout the file is
# sourced from (the main checkout or any worktree) and cannot go stale: C:/a/b -> //mnt/c/a/b.
# Double slash on purpose: Git Bash (MSYS) rewrites a standalone /mnt/... argument into
# C:/Program Files/Git/mnt/... before wsl.exe sees it; //mnt/... arrives intact (verified).
# CDPATH= and >/dev/null: with CDPATH set (CDPATH=. in a login profile is enough) `cd` echoes
# the directory it entered, which would be glued into the path and add a second line.
_spark_root=$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -W)
_spark_drive=$(printf '%s' "${_spark_root%%:*}" | tr 'A-Z' 'a-z')
REPO_WSL="//mnt/$_spark_drive${_spark_root#?:}"
unset _spark_root _spark_drive
RUN_DATE=$(date +%F)
export SSH SCP REPO_WSL RUN_DATE
