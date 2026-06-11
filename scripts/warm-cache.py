#!/usr/bin/env python3
"""Warm the default Schoharie-area results cache if missing."""

from server import DEFAULT_LAT, DEFAULT_LON, DEFAULT_RADIUS_KM, get_cached_results, run_analysis


def main():
    if get_cached_results(DEFAULT_LAT, DEFAULT_LON, DEFAULT_RADIUS_KM):
        print("Default cache already exists")
        return
    print("Warming default cache…")
    run_analysis(DEFAULT_LAT, DEFAULT_LON, DEFAULT_RADIUS_KM)
    print("Default cache ready")


if __name__ == "__main__":
    main()
