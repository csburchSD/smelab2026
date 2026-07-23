#!/usr/bin/env python3
"""Lab 3: lab_events.

lab_events is written to live by this script two ways:
  - SEQUENTIAL: every _id is a zero-padded, strictly increasing counter
    (the same shape as an auto-increment key or an ISO timestamp used
    as a primary key)
  - RANDOM: every _id is a UUID4, spreading keys across the whole
    keyspace

Default (no arguments): runs both and reports insert latency/throughput
for each. The collection is cleared once at the start of the run, then
both scenarios' documents accumulate together in lab_events (so you can
inspect either set of _ids afterward) -- it's not cleared between
scenarios.

Note: this script measures client-observed insert latency/throughput as
a proxy signal. Treat the absolute numbers cautiously -- what matters is
comparing scenarios against each other, not the raw milliseconds.

Your turn: if you have a hypothesis for a key strategy that keeps some
ordering while avoiding whatever RANDOM avoids, test it with:

    python 03_lab_events.py --shard-prefixes N

which runs a third scenario alongside the built-in two: a sequential
counter with a random prefix drawn from [0, N), added to the same
comparison table. Try different N and see how it lands relative to the
other two rows.
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
        "--shard-prefixes", type=int, default=0,
        help="Also run a third scenario: a sequential counter salted with a random "
             "prefix in [0, N). N=0 (default) skips this scenario.",
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


def run_scenario(coll, id_factory):
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [
            pool.submit(timed_insert, coll, id_factory())
            for _ in range(WRITERS * OPS_PER_WRITER)
        ]
        latencies = [f.result() for f in futures]
    wall = time.perf_counter() - start
    return latencies, wall


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
    rows = []
    for label, id_factory in scenarios:
        console.print(f"Inserting {WRITERS * OPS_PER_WRITER} docs with {label} "
                      f"via {WRITERS} concurrent writers...")
        latencies, wall = run_scenario(coll, id_factory)
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
