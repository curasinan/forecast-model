SSH=/c/Windows/System32/OpenSSH/ssh.exe   # Windows OpenSSH: reads ~/.ssh/config (Host spark)
SCP=/c/Windows/System32/OpenSSH/scp.exe
_spark_root=$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -W)
_spark_drive=$(printf '%s' "${_spark_root%%:*}" | tr 'A-Z' 'a-z')
REPO_WSL="//mnt/$_spark_drive${_spark_root#?:}"
unset _spark_root _spark_drive
RUN_DATE=$(date +%F)
export SSH SCP REPO_WSL RUN_DATE
