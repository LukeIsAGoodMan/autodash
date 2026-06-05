"""Launch the Dash app. Thin wrapper so Task Scheduler / desktop shortcuts
have a single, stable file path to call."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.app import main

if __name__ == "__main__":
    main()
