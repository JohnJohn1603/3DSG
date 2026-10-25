#!/usr/bin/env python3
"""Run the locked ordered replay of all 13 persisted T1.1/v8 keyframes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integration_v2.v8_ordered_replay import main


if __name__ == "__main__":
    raise SystemExit(main())
