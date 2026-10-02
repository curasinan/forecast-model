# Shared shell variables for the Spark work. From the worktree, in Git Bash:
#   source scripts/spark/env.sh
# Shell state does not survive between Claude's Bash tool calls, hence one file.
SSH=/c/Windows/System32/OpenSSH/ssh.exe   # Windows OpenSSH: reads ~/.ssh/config (Host spark)
SCP=/c/Windows/System32/OpenSSH/scp.exe
# Double slash on purpose: Git Bash (MSYS) rewrites a standalone /mnt/... argument into
# C:/Program Files/Git/mnt/... before wsl.exe sees it; //mnt/... arrives intact (verified).
REPO_WSL=//mnt/c/Users/asus/OneDrive/Desktop/new_work/.claude/worktrees/spark-research-split
RUN_DATE=$(date +%F)
export SSH SCP REPO_WSL RUN_DATE
