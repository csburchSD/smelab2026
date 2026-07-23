#!/usr/bin/env python3
"""Lab 7: lab_bulkwrite_scratch.

Two related but different things, worth telling apart:

  BATCH  = one atomic multi-document server-side operation (insert_many /
           bulk_write). All-or-nothing.
  BULK   = a client-side pattern of many independent single-document writes
           issued concurrently (e.g. a thread pool). No cross-document
           atomicity -- one failure doesn't abort the rest.

This script writes the same total number of documents five ways: serial
(baseline), batched at three sizes, and bulk (parallel individual writes),
and reports wall time and throughput for each so you can see where each
one lands -- draw your own conclusion about how batch size and parallelism
each affect the result before checking it against a real workload's
atomicity requirements.

Default (no arguments): batch sizes 10, 100, 500.

Your turn:

    python 07_batch_vs_bulk_writes.py --batch-sizes 5,50,1000

tests your own set of batch sizes against the same serial/bulk baselines.
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.table import Table

from _internal.lab_config import get_lab_db

console = Console()

TOTAL_DOCS = 1000
BULK_WORKERS = 20
PAYLOAD = {"note": "x" * 200}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--batch-sizes", type=str, default="10,100,500",
        help="Comma-separated batch sizes to test, e.g. 5,50,1000 (default: 10,100,500).",
    )
    return parser.parse_args()


def warm_up(coll):
    """One throwaway concurrent burst so no scenario below pays the same
    first-touch collection warm-up cost 01_lab_counters.py's
    run_spread() had to control for -- see its docstring comment."""
    with ThreadPoolExecutor(max_workers=BULK_WORKERS) as pool:
        futures = [pool.submit(coll.insert_one, dict(PAYLOAD)) for _ in range(BULK_WORKERS * 2)]
        for f in futures:
            f.result()


def run_serial(coll, n):
    start = time.perf_counter()
    for _ in range(n):
        coll.insert_one(dict(PAYLOAD))
    return time.perf_counter() - start


def run_batched(coll, n, batch_size):
    start = time.perf_counter()
    remaining = n
    while remaining > 0:
        size = min(batch_size, remaining)
        coll.insert_many([dict(PAYLOAD) for _ in range(size)])
        remaining -= size
    return time.perf_counter() - start


def run_bulk_parallel(coll, n, workers):
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(coll.insert_one, dict(PAYLOAD)) for _ in range(n)]
        for f in futures:
            f.result()
    return time.perf_counter() - start


def summarize(name, batch_size, n, wall):
    return {
        "scenario": name,
        "batch_size": str(batch_size) if batch_size else "-",
        "ops": n,
        "wall_s": round(wall, 3),
        "ops_per_sec": round(n / wall, 1) if wall else float("nan"),
    }


def print_table(rows):
    table = Table(title=f"Inserting {TOTAL_DOCS} docs: serial vs. batched vs. bulk (parallel)")
    table.add_column("Scenario")
    table.add_column("Batch size", justify="right")
    table.add_column("Ops", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("ops/sec", justify="right")

    for row in rows:
        table.add_row(row["scenario"], row["batch_size"], str(row["ops"]),
                      str(row["wall_s"]), str(row["ops_per_sec"]))
    console.print(table)


def main():
    args = parse_args()
    batch_sizes = [int(s.strip()) for s in args.batch_sizes.split(",") if s.strip()]

    console.print("[bold cyan]Lab 7: lab_bulkwrite_scratch[/]\n")
    db = get_lab_db()
    coll = db["lab_bulkwrite_scratch"]
    console.print(f"[dim]Collection: {db.name}.lab_bulkwrite_scratch (scratch, dropped after this run)[/]\n")

    coll.drop()
    warm_up(coll)

    rows = []

    console.print(f"SERIAL: {TOTAL_DOCS} inserts, one at a time...")
    rows.append(summarize("SERIAL", None, TOTAL_DOCS, run_serial(coll, TOTAL_DOCS)))

    for size in batch_sizes:
        console.print(f"BATCHED: {TOTAL_DOCS} inserts via insert_many, batch size {size}...")
        rows.append(summarize("BATCHED", size, TOTAL_DOCS, run_batched(coll, TOTAL_DOCS, size)))

    console.print(f"BULK: {TOTAL_DOCS} inserts, {BULK_WORKERS} concurrent workers, no batching...\n")
    rows.append(summarize(f"BULK ({BULK_WORKERS} workers)", None, TOTAL_DOCS,
                           run_bulk_parallel(coll, TOTAL_DOCS, BULK_WORKERS)))

    coll.drop()
    print_table(rows)
    console.print(
        "\n[dim]Same total doc count in every row. BATCHED rows are atomic per batch; BULK is not "
        "atomic across documents at all, but parallelizes instead of queuing.[/]"
    )


if __name__ == "__main__":
    main()
