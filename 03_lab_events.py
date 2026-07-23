#!/usr/bin/env python3
"""Lab 3: lab_events.

lab_events is written to live by this script three ways:
  - SEQUENTIAL: every _id is a zero-padded, strictly increasing counter
    (the same shape as an auto-increment key or an ISO timestamp used
    as a primary key)
  - RANDOM: every _id is a UUID4, spreading keys across the whole
    keyspace
  - SALTED: a sequential counter with a random prefix drawn from [0, N),
    keeping a recoverable per-prefix ordering while still spreading
    writes across N ranges

Default (no arguments): runs all three and reports insert latency/throughput
for each. The collection is cleared once at the start of the run, then all
three scenarios' documents accumulate together in lab_events (so you can
inspect any of the _id sets afterward) -- it's not cleared between
scenarios.

Note: this script measures client-observed insert latency/throughput as
a proxy signal. Treat the absolute numbers cautiously -- what matters is
comparing scenarios against each other, not the raw milliseconds.

Your turn: try a different prefix count and see how it lands relative to
the other rows:

    python 03_lab_events.py --shard-prefixes N
"""

import argparse
import itertools
import random
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.table import Table

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()

WRITERS = 20
OPS_PER_WRITER = 15

_seq_lock = threading.Lock()
_seq = itertools.count()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--shard-prefixes", type=int, default=3,
        help="Prefix count for the SALTED scenario: a sequential counter salted with "
             "a random prefix in [0, N) (default: 3). N=0 skips the SALTED scenario "
             "entirely.",
    )
    parser.add_argument(
        "--ops-per-writer", type=int, default=OPS_PER_WRITER,
        help=f"Ops per concurrent writer per scenario (default: {OPS_PER_WRITER}, "
             f"i.e. {WRITERS * OPS_PER_WRITER} total ops per scenario). A quick, small "
             "burst may not run long enough to expose a real hot-range effect -- push "
             "this up to sustain load over a longer window and see if a gap emerges.",
    )
    return parser.parse_args()


def next_sequential_id():
    with _seq_lock:
        return f"evt-{next(_seq):012d}"


def make_salted_id_factory(n):
    def factory():
        with _seq_lock:
            c = next(_seq)
        prefix = random.randint(0, n - 1)
        return f"{prefix}-evt-{c:012d}"
    return factory


def timed_insert(coll, doc_id):
    start = time.perf_counter()
    coll.insert_one({"_id": doc_id, "payload": "x" * 200, "created_at": time.time()})
    return time.perf_counter() - start


def run_scenario(coll, id_factory, ops_per_writer=OPS_PER_WRITER):
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [
            pool.submit(timed_insert, coll, id_factory())
            for _ in range(WRITERS * ops_per_writer)
        ]
        latencies = [f.result() for f in futures]
    wall = time.perf_counter() - start
    return latencies, wall


def warm_up(coll):
    """This backend (or something about this environment) pays a fixed ~5s
    cost on one op the first time real concurrent load hits a given region
    of the keyspace -- observed directly, reproducibly: a random-keyed
    warm-up burst does NOT clear it for SEQUENTIAL, because uuid4() keys
    and "evt-<counter>" keys occupy completely different, non-overlapping
    regions of the keyspace. Warming up with random keys only pre-touches
    the region RANDOM itself will reuse; it never touches the specific
    ascending tail SEQUENTIAL is about to extend. So warm up using the same
    id shape as each real scenario, not just one random burst -- this uses
    next_sequential_id() directly, which shares SEQUENTIAL's own counter,
    so the real scenario continues the same ascending tail these warm-up
    ops just extended (a closer proxy for a real "constantly-growing tail"
    workload than resetting to 0 would be, anyway)."""
    run_scenario(coll, lambda: str(uuid.uuid4()))
    run_scenario(coll, next_sequential_id)
    coll.delete_many({})


def summarize(name, latencies, wall):
    ms = sorted(l * 1000 for l in latencies)
    return {
        "scenario": name,
        "ops": len(ms),
        "wall_s": round(wall, 3),
        "p50_ms": round(statistics.median(ms), 1),
        "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1),
        "max_ms": round(ms[-1], 1),
        "ops_per_sec": round(len(ms) / wall, 1),
    }


def main():
    args = parse_args()
    console.print("[bold cyan]Lab 3: lab_events[/]\n")
    db = get_lab_db()
    coll = db[LAB_COLLECTIONS["events"]]
    console.print(f"[dim]Collection: {db.name}.{coll.name}[/]\n")

    scenarios = [("SEQUENTIAL _id", next_sequential_id), ("RANDOM _id", lambda: str(uuid.uuid4()))]
    if args.shard_prefixes > 0:
        scenarios.append((f"SALTED ({args.shard_prefixes} prefixes)", make_salted_id_factory(args.shard_prefixes)))

    coll.delete_many({})
    warm_up(coll)
    rows = []
    for label, id_factory in scenarios:
        console.print(f"Inserting {WRITERS * args.ops_per_writer} docs with {label} "
                      f"via {WRITERS} concurrent writers...")
        latencies, wall = run_scenario(coll, id_factory, args.ops_per_writer)
        rows.append(summarize(label, latencies, wall))

    table = Table(title="Insert latency across key strategies")
    table.add_column("Scenario")
    table.add_column("Ops", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("max (ms)", justify="right")
    table.add_column("ops/sec", justify="right")

    for row in rows:
        table.add_row(row["scenario"], str(row["ops"]), str(row["wall_s"]),
                      str(row["p50_ms"]), str(row["p95_ms"]), str(row["max_ms"]),
                      str(row["ops_per_sec"]))

    console.print("\n")
    console.print(table)
    console.print(
        "\n[dim]Client-observed timing is a proxy signal here -- compare the scenarios "
        "against each other rather than reading too much into the absolute numbers.[/]"
    )


if __name__ == "__main__":
    main()
