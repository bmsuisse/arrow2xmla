"""Parity tests: the Rust crate against a reference implementation in Python.

Dev-only coupling: the reference is OneView's pure-Python XMLA writer, found through ONEVIEW_PATH (a checkout of it);
the library itself knows nothing of OneView. Run with OneView's interpreter so its imports resolve:

    ONEVIEW_PATH=../OneView PYTHONPATH=python ../OneView/.venv/bin/python -m pytest tests/parity
"""

import os
import sys

import pytest

ONEVIEW = os.environ.get("ONEVIEW_PATH")
if not ONEVIEW:
    pytest.skip("set ONEVIEW_PATH to a OneView checkout to run the parity tests", allow_module_level=True)

os.environ.setdefault("PUBLIC_API_ORIGINS", "[]")
os.environ.setdefault("AI_ASK_PER_USER_PER_HOUR", "60")
sys.path.insert(0, os.path.abspath(ONEVIEW))
