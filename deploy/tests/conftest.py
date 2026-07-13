"""Pytest configuration shared by every test module.

Two moving parts:

- Both `dashboard-context/src` and `bootstrap/` are put on sys.path so tests
  can `import dashboard_context.*` and `import register` / `import mint_token`
  without installing them.
- pytest-asyncio is put in auto mode so `async def test_*` functions work
  without individual @pytest.mark.asyncio decorators.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # deploy/
sys.path.insert(0, str(ROOT / "dashboard-context" / "src"))
sys.path.insert(0, str(ROOT / "bootstrap"))
