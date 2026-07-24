#!/usr/bin/env python3
"""Lab 3: lab_events.

Firestore's own guidance warns against monotonically increasing (or
decreasing) keys for high-throughput writes: an auto-increment counter or an
ISO timestamp used as the primary key means every new write lands at the
same, constantly-advancing edge of the keyspace. The backend has to keep
re-splitting that one edge to keep serving it as it grows, instead of
spreading the work across ranges it already knows about. A randomized or
salted key has no single "latest" edge to concentrate on, so there's nothing
to re-split.

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
for each, plus a "drift" column -- each scenario's own second-half p50
latency compared to its first-half p50, within that same run. That's the
more reliable signal to watch: a monotonic key's hot edge should get harder
to serve as it accumulates more writes (drift trending up), while a
randomized/salted key's load stays spread out and flat -- and unlike a
straight cross-scenario comparison, a within-scenario drift isn't thrown off
by the one-off startup latency artifact documented below (median is immune
to a single outlier either way, but here it also means SEQUENTIAL isn't
being compared against RANDOM/SALTED's own separate outlier risk). The
collection is cleared once at the start of the run, then all three
scenarios' documents accumulate together in lab_events (so you can inspect
any of the _id sets afterward) -- it's not cleared between scenarios.

Note: at this lab's workshop-safe scale, this backend can auto-scale fast
enough that the *absolute* latency gap between scenarios isn't always
visible run to run -- Firestore's hot-ranging guidance describes a
production/sustained-throughput failure mode, not necessarily something a
few thousand ops from one process reliably reproduces on demand. Treat the
numbers here as a proxy signal for the mechanism, not a guarantee -- lean on
understanding *why* a monotonic key is risky in production over expecting
this script to prove it every single run.

Your turn: try a different prefix count, or push the load higher and see if
drift becomes clearer:

    python 03_lab_events.py --shard-prefixes N
    python 03_lab_events.py --ops-per-writer 500
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
    return start, time.perf_counter() - start


def run_scenario(coll, id_factory, ops_per_writer=OPS_PER_WRITER):
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [
            pool.submit(timed_insert, coll, id_factory())
            for _ in range(WRITERS * ops_per_writer)
        ]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - start
    # f.result() above returns in submission order, not the order ops actually
    # ran/finished -- sort by each op's own start time so "first half vs
    # second half" below reflects chronological progress through the run, not
    # submission order (the two mostly line up with a fixed worker pool, but
    # this makes it exact).
    results.sort(key=lambda r: r[0])
    latencies = [latency for _, latency in results]
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
    # latencies arrives in chronological (start-time) order from
    # run_scenario -- keep a chronological copy for the drift split before
    # sorting by value for the percentile columns.
    ms_chrono = [l * 1000 for l in latencies]
    ms = sorted(ms_chrono)
    half = len(ms_chrono) // 2
    first_half_p50 = statistics.median(ms_chrono[:half])
    second_half_p50 = statistics.median(ms_chrono[half:])
    drift_pct = (second_half_p50 - first_half_p50) / first_half_p50 * 100
    return {
        "scenario": name,
        "ops": len(ms),
        "wall_s": round(wall, 3),
        "p50_ms": round(statistics.median(ms), 1),
        "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1),
        "max_ms": round(ms[-1], 1),
        "ops_per_sec": round(len(ms) / wall, 1),
        # 2nd-half p50 vs 1st-half p50, within this scenario's own run -- a
        # monotonic key's hot edge getting harder to serve as it grows shows
        # up as this trending positive; a spread-out key stays flat. Median
        # of each half keeps this immune to the one-off startup artifact
        # (see the module docstring) the same way p50 already is overall.
        "drift_pct": round(drift_pct, 1),
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
    table.add_column("Drift (2nd half vs 1st)", justify="right")

    for row in rows:
        table.add_row(row["scenario"], str(row["ops"]), str(row["wall_s"]),
                      str(row["p50_ms"]), str(row["p95_ms"]), str(row["max_ms"]),
                      str(row["ops_per_sec"]), f"{row['drift_pct']:+.1f}%")

    console.print("\n")
    console.print(table)
    console.print(
        "\n[dim]Client-observed timing is a proxy signal here -- compare the scenarios "
        "against each other rather than reading too much into the absolute numbers. Drift "
        "compares each scenario's own second-half p50 to its first-half p50 -- a monotonic "
        "key's hot edge getting harder to serve as it grows should trend positive here even "
        "on a run where the scenarios' overall latencies don't cleanly separate from each "
        "other.[/]"
    )


if __name__ == "__main__":
    main()
