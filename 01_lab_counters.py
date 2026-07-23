#!/usr/bin/env python3
"""Lab 1: lab_counters.

lab_counters holds one document (`global_stats`) that every "request" in
this simulation increments with $inc -- e.g. a naive global page-view
counter. This script measures per-operation latency for a batch of
concurrent $inc calls, either all against that one document, or spread
across a number of documents you choose.

Default (no arguments): runs the built-in comparison --  the same total
number of ops spread across WRITERS separate documents, vs. all of them
against the one shared document.

    python 01_lab_counters.py

Your turn: test your own hypothesis for how the numbers change with:

    python 01_lab_counters.py --docs N

which spreads the same total workload across N documents (N=1 reproduces
the shared-document case; try other values and compare the numbers
yourself). This script doesn't reconstitute a single total from N
documents for you -- if your fix needs one, that's part of what to work
out.
"""

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from pymongo.errors import OperationFailure
from rich.console import Console
from rich.table import Table

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()

WRITERS = 20
OPS_PER_WRITER = 5


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--docs", type=int, default=None,
        help="Spread the same total workload across this many documents instead of "
             "running the built-in comparison. Try your own values.",
    )
    return parser.parse_args()


def timed_increment(coll, doc_id):
    """Time one $inc, tolerating the ABORTED-on-contention error.

    retryWrites=false is required on this connection, so the driver does not
    retry a failed write for us -- we catch it here instead of letting one
    failed op crash the whole benchmark.
    """
    start = time.perf_counter()
    try:
        coll.update_one({"_id": doc_id}, {"$inc": {"hits": 1}, "$set": {"last_updated": time.time()}})
        ok = True
    except OperationFailure:
        ok = False
    return time.perf_counter() - start, ok


def run_spread(db, num_docs):
    """Same total workload (WRITERS x OPS_PER_WRITER ops), spread across num_docs documents."""
    coll = db["lab_counters_experiment"]
    coll.drop()
    coll.insert_many([{"_id": f"ctr-{i}", "hits": 0} for i in range(num_docs)])

    def burst():
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            futures = [
                pool.submit(timed_increment, coll, f"ctr-{writer % num_docs}")
                for writer in range(WRITERS)
                for _ in range(OPS_PER_WRITER)
            ]
            return [f.result() for f in futures]

    # A brand-new collection's first concurrent burst pays a one-time backend
    # routing/split warm-up cost that run_hot()'s document never pays (it's been
    # hit by every prior run of this script). A single untimed warm-up burst
    # measurably clears that cost -- a sequential touch-once per doc does not --
    # so the timed comparison below reflects contention, not cold-start.
    burst()

    start = time.perf_counter()
    results = burst()
    wall = time.perf_counter() - start

    coll.drop()
    return results, wall


def run_hot(db):
    coll = db[LAB_COLLECTIONS["counters"]]

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [
            pool.submit(timed_increment, coll, "global_stats")
            for _ in range(WRITERS)
            for _ in range(OPS_PER_WRITER)
        ]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - start
    return results, wall


def summarize(name, results, wall):
    ms = sorted(latency * 1000 for latency, ok in results if ok)
    errors = sum(1 for _, ok in results if not ok)
    return {
        "scenario": name,
        "ops": len(results),
        "errors": errors,
        "wall_s": round(wall, 3),
        "p50_ms": round(statistics.median(ms), 1) if ms else float("nan"),
        "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1) if ms else float("nan"),
        "max_ms": round(ms[-1], 1) if ms else float("nan"),
        "ops_per_sec": round(len(ms) / wall, 1),
    }


def print_table(rows):
    table = Table(title="$inc latency")
    table.add_column("Scenario")
    table.add_column("Ops", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("max (ms)", justify="right")
    table.add_column("ops/sec", justify="right")

    for row in rows:
        table.add_row(row["scenario"], str(row["ops"]), str(row["errors"]), str(row["wall_s"]),
                      str(row["p50_ms"]), str(row["p95_ms"]), str(row["max_ms"]),
                      str(row["ops_per_sec"]))
    console.print(table)


def main():
    args = parse_args()
    console.print("[bold cyan]Lab 1: lab_counters[/]\n")
    db = get_lab_db()

    if args.docs is not None:
        console.print(f"[dim]Collection: {db.name}.lab_counters_experiment "
                      f"(scratch, dropped after this run)[/]")
        console.print(f"Running {WRITERS * OPS_PER_WRITER} ops ({WRITERS} concurrent writers) "
                      f"against {args.docs} document(s)...\n")
        results, wall = run_spread(db, args.docs)
        print_table([summarize(f"{args.docs} doc(s)", results, wall)])
        console.print(
            "\n[dim]Errors = writes that failed after exhausting retries "
            "(retryWrites=false means the driver won't retry them for you).[/]"
        )
        return

    console.print(f"[dim]Collections: {db.name}.lab_counters_experiment (scratch COLD docs, "
                  f"dropped after this run) and {db.name}.lab_counters (the real HOT doc, "
                  f"_id=\"global_stats\")[/]")

    console.print(f"Running {WRITERS} concurrent writers x {OPS_PER_WRITER} ops "
                  f"against {WRITERS} separate documents (COLD)...")
    cold_lat, cold_wall = run_spread(db, WRITERS)

    console.print(f"Running {WRITERS} concurrent writers x {OPS_PER_WRITER} ops "
                  f"against ONE shared document (HOT)...\n")
    hot_lat, hot_wall = run_hot(db)

    print_table([summarize("COLD (20 docs)", cold_lat, cold_wall),
                 summarize("HOT (1 doc)", hot_lat, hot_wall)])
    console.print(
        "\n[dim]Same total work, same cluster. Errors = writes that failed after "
        "exhausting retries (retryWrites=false means the driver won't retry them "
        "for you). Try `--docs N` for values between 1 and 20 to explore further.[/]"
    )


if __name__ == "__main__":
    main()
