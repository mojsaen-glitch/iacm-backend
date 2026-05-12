import sys
import os

# Add the backend root to path so "app" package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.main import app  # noqa: F401 — Vercel picks up this `app`
