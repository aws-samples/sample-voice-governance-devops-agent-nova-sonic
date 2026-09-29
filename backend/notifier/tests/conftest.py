"""Pytest bootstrap for the Notifier Lambda test suite.

Prepends the repository ``backend/`` directory to ``sys.path`` so the shared
backend primitives package (``shared``) resolves when the suite runs from
``backend/notifier``; the ``src`` package already resolves from the package
root that pytest prepends automatically.
"""

import sys
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[2])

if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
