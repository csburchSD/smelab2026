#!/usr/bin/env python3
"""Restore the anti-patterns lab to its pristine broken state.

Not meant to be run directly -- invoked via `provision_lab.py --reset`
(or `--run-all`). If you need it standalone, run `python -m
_internal.reset_lab` from the lab directory.

Run this between training sessions or whenever a trainee's fix (new
indexes, restructured documents, etc.) needs to be wiped so the next
cohort starts from the same four anti-patterns. Dropping each lab_
collection also drops any indexes a trainee created on it.
"""

from rich.console import Console
from rich.panel import Panel

from _internal import setup_lab
from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()


def main():
    console.print(Panel.fit(
        "[bold yellow]Resetting Firestore Anti-Patterns Lab[/]\n"
        "Dropping lab_ collections (and any trainee-added indexes)...",
        border_style="yellow",
    ))

    db = get_lab_db()
    for name in LAB_COLLECTIONS.values():
        db[name].drop()

    setup_lab.main()


if __name__ == "__main__":
    main()
