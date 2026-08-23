"""Launch the prediction system dashboard.

Usage:
    python scripts/run_dashboard.py              # Interactive mode (menu)
    python scripts/run_dashboard.py --once        # Run once and display
    python scripts/run_dashboard.py --scheduler   # Auto-run every 6h
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Fix Windows Unicode
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ui.cli_dashboard import Dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


async def main():
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("scripts/run_dashboard.py")
    mode = "interactive"
    if len(sys.argv) > 1:
        if sys.argv[1] == "--once":
            mode = "oneshot"
        elif sys.argv[1] == "--scheduler":
            mode = "scheduler"

    dashboard = Dashboard()

    if mode == "oneshot":
        await dashboard.run_oneshot()
    elif mode == "scheduler":
        await dashboard._run_scheduler()
    else:
        await dashboard.run_interactive()


if __name__ == "__main__":
    asyncio.run(main())
