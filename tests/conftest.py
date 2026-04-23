"""Shared fixtures and pytest configuration."""
import sys
from pathlib import Path

# Ensure project root is importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
