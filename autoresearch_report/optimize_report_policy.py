#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autoresearch_report import benchmark_report


def main():
    argv = sys.argv[1:]
    if "--apply" not in argv:
        argv.append("--apply")
    if "--limit" not in argv:
        argv.extend(["--limit", "2"])
    sys.argv = [sys.argv[0], *argv]
    benchmark_report.main()


if __name__ == "__main__":
    main()
