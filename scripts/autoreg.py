#!/usr/bin/env python3
"""Launcher so the skill can be used without installing a package."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from autoreg_pkg.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
