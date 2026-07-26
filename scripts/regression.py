#!/usr/bin/env python3
"""Repository-local entry point for the Nico regression catalog."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "backend" / "src"))

from nico_agent.regressions.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
