#!/usr/bin/env python3
"""Reset the lab and run all seven anti-pattern demonstrations in sequence.

Not meant to be run directly -- invoked via `provision_lab.py --run-all`.
If you need it standalone, run `python -m _internal.run_all` from the lab
directory (not `python _internal/run_all.py` -- the numbered scripts below
are only importable when the lab directory itself is on sys.path).

Convenience entry point for a facilitator doing a dry run before a
session, or for showing all seven symptoms back-to-back live.
"""

import importlib

from rich.console import Console
from rich.rule import Rule

from _internal import reset_lab

console = Console()

DEMO_MODULES = [
    "01_lab_counters",
    "02_lab_devices",
    "03_lab_events",
    "04_inefficient_compound_fields",
    "05_lab_traffic_scratch",
    "06_large_reads_pagination",
    "07_batch_vs_bulk_writes",
]


def main():
    reset_lab.main()

    for name in DEMO_MODULES:
        console.print(Rule(f"[bold]{name}[/]", style="cyan"))
        module = importlib.import_module(name)
        module.main()
        console.print()


if __name__ == "__main__":
    main()
