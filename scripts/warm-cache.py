#!/usr/bin/env python3
"""Warm the default cache if missing. Prefer: python scripts/build_cache.py offline."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from build_cache import build  # noqa: E402
from server import DEFAULT_LAT, DEFAULT_LON, DEFAULT_RADIUS_KM  # noqa: E402


def main():
    build(DEFAULT_LAT, DEFAULT_LON, DEFAULT_RADIUS_KM)


if __name__ == "__main__":
    main()
