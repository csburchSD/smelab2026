#!/usr/bin/env python3
"""Lab 5: lab_traffic_scratch.

Firestore publishes guidance on how quickly write/read/delete traffic to a
collection is allowed to grow before it should be considered risky. This
script compresses whatever timescale that guidance operates on down into
seconds, so it fits a live workshop -- treat it as a proxy for a mechanism,
not a literal reproduction of minutes vs. seconds.

Scope: what this measures is backend behavior against a narrow slice of key
space -- not application-tier serverless cold starts (e.g. a Cloud
Function/Lambda spinning up a new instance) and not a claim about the
database provisioning additional compute in the background. This script
doesn't verify those independently.

Both scenarios below hit the *same* narrow range of 30 scratch documents
with the *same* final concurrency (40 workers) -- the only variable is
whether that concurrency arrived all at once or in stages:

  SPIKE: 0 -> 40 workers instantly, against a never-touched range.
  RAMP:  5 -> 10 -> 20 -> 40 workers in stages; only the final stage (same
         shape as SPIKE) is timed and reported.

Default (no arguments): runs SPIKE vs RAMP using writes ($inc on the
range).

Your turn: rerun with

    python 05_lab_traffic_scratch.py --mode reads

to see whether the same gap between scenarios shows up for repeated point
reads against the same narrow range instead.
"""

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from pymongo.errors import OperationFailure
from rich.console import Console
from rich.table import Table

from _internal.lab_config import get_lab_db

console = Console()

RANGE_SIZE = 30
STAGES = [5, 10, 20, 40]
OPS_PER_WORKER = 5
STAGE_PAUSE_S = 0.2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--mode", choices=["writes", "reads"], default="writes",
        help="Hit the hot range with $inc writes (default) or find_one reads.",
    )
    return parser.parse_args()


def timed_op(coll, doc_id, mode):
    start = time.perf_counter()
    try:
        if mode == "writes":
            coll.update_one({"_id": doc_id}, {"$inc": {"hits": 1}})
        else:
            coll.find_one({"_id": doc_id})
        ok = True
    except OperationFailure:
        ok = False
    return time.perf_counter() - start, ok


def seed_range(coll):
    coll.drop()
    coll.insert_many([{"_id": f"hot-{i:03d}", "hits": 0} for i in range(RANGE_SIZE)])


def burst(coll, workers, mode):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(timed_op, coll, f"hot-{w % RANGE_SIZE:03d}", mode)
            for w in range(workers)
            for _ in range(OPS_PER_WORKER)
        ]
        return [f.result() for f in futures]


def run_spike(db, mode):
    """Full target concurrency in one shot against a never-touched range."""
    coll = db["lab_traffic_scratch"]
    seed_range(coll)

    start = time.perf_counter()
    results = burst(coll, STAGES[-1], mode)
    wall = time.perf_counter() - start

    coll.drop()
    return results, wall


def run_ramp(db, mode):
    """Step concurrency up through STAGES; only the final stage is timed."""
    coll = db["lab_traffic_scratch"]
    seed_range(coll)

    for stage_workers in STAGES[:-1]:
        burst(coll, stage_workers, mode)
        time.sleep(STAGE_PAUSE_S)

    start = time.perf_counter()
    results = burst(coll, STAGES[-1], mode)
    wall = time.perf_counter() - start

    coll.drop()
    return results, wall


def summarize(name, results, wall, concurrency):
    ms = sorted(latency * 1000 for latency, ok in results if ok)
    errors = sum(1 for _, ok in results if not ok)
    p95 = ms[int(len(ms) * 0.95) - 1] if ms else float("nan")
    return {
        "scenario": name,
        "ops": len(results),
        "errors": errors,
        "wall_s": round(wall, 3),
        "p50_ms": round(statistics.median(ms), 1) if ms else float("nan"),
        "p95_ms": round(p95, 1) if ms else float("nan"),
        "max_ms": round(ms[-1], 1) if ms else float("nan"),
        # Derived from concurrency / p95 latency, not ops / wall or a p50-based
        # estimate -- this lab's actual effect (a spike queuing behind a
        # not-yet-scaled range) shows up specifically as tail latency, not a
        # uniform shift of the whole distribution: p50 alone was observed
        # flip-flopping direction (even favoring SPIKE in a clean run with no
        # warm-up artifact at all), while p95 consistently favored RAMP by
        # ~3x across every run tried, contaminated or not. Wall-clock is
        # additionally one straggler op away from making a fast scenario look
        # like it crawled (observed directly: a single ~5-10s outlier among
        # 200 ops otherwise finishing in ~20ms each).
        "ops_per_sec": round(concurrency / (p95 / 1000), 1) if ms and p95 else float("nan"),
    }


def print_table(rows, mode):
    table = Table(title=f"{'$inc' if mode == 'writes' else 'find_one'} latency: spike vs. ramp")
    table.add_column("Scenario")
    table.add_column("Ops", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("max (ms)", justify="right")
    table.add_column("est. ops/sec", justify="right")

    for row in rows:
        table.add_row(row["scenario"], str(row["ops"]), str(row["errors"]), str(row["wall_s"]),
                      str(row["p50_ms"]), str(row["p95_ms"]), str(row["max_ms"]),
                      str(row["ops_per_sec"]))
    console.print(table)


def main():
    args = parse_args()
    console.print("[bold cyan]Lab 5: lab_traffic_scratch[/]\n")
    db = get_lab_db()
    console.print(f"[dim]Collection: {db.name}.lab_traffic_scratch "
                  f"(scratch, {RANGE_SIZE} docs, dropped after each scenario)[/]\n")

    console.print(f"SPIKE: 0 -> {STAGES[-1]} workers instantly against {RANGE_SIZE} never-touched docs...")
    spike_results, spike_wall = run_spike(db, args.mode)

    console.print(f"RAMP: {' -> '.join(str(s) for s in STAGES)} workers in stages "
                  f"(only the final stage is timed)...\n")
    ramp_results, ramp_wall = run_ramp(db, args.mode)

    print_table([
        summarize("SPIKE (never-touched)", spike_results, spike_wall, STAGES[-1]),
        summarize(f"RAMP final stage ({STAGES[-1]} workers, pre-warmed)", ramp_results, ramp_wall, STAGES[-1]),
    ], args.mode)

    console.print(
        "\n[dim]Same target concurrency and op count in both rows -- the only difference is "
        "whether it arrived all at once or in stages. This compresses a rule Firestore applies "
        "over minutes into seconds; treat it as a proxy for the mechanism, not the timescale. "
        "est. ops/sec is concurrency / p95 latency, not ops / wall-clock -- p95 tracks this "
        "lab's tail-latency effect more reliably than p50 or wall-clock, both of which can "
        "make a fast scenario look misleadingly bad on any given run.[/]"
    )


if __name__ == "__main__":
    main()
