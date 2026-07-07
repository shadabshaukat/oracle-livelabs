#!/usr/bin/env python3
"""Extract a verified .tar.zst using Python 3.14's standard library."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("destination")
    args = parser.parse_args()

    destination = Path(args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.archive, mode="r:zst") as archive:
        archive.extractall(destination, filter="data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
