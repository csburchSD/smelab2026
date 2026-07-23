#!/usr/bin/env python3
"""Lab 2: lab_devices.

lab_devices has four documents pre-loaded with a `readings` array of
different sizes (0, 200, 2000, 9000 entries) -- simulating a device that
has been appending sensor readings into one document for a longer or
shorter amount of time.

This script measures, for each existing array size:
  - the document's on-the-wire size (via BSON encoding)
  - the latency of a single $push to append one more reading

Default (no arguments): reports those four pre-seeded sizes, then builds
a scratch document to see what happens at the far end of that trend.

Your turn: if you're considering a schema change with a size limit or
bucket boundary in mind, test a specific array length directly:

    python 02_lab_devices.py --test-sizes 50,500,5000

This builds a scratch document with each given number of pre-existing
readings and reports its size and append latency, same as the table
above -- compare the numbers yourself.
"""

import argparse
import time

from bson import BSON
from pymongo.errors import DocumentTooLarge, OperationFailure
from rich.console import Console
from rich.table import Table

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()

READING = {"ts": None, "value": 21.5, "unit": "celsius", "quality": "good"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--test-sizes", type=str, default=None,
        help="Comma-separated array lengths to test in a scratch document, e.g. 50,500,5000",
    )
    parser.add_argument(
        "--skip-overflow", action="store_true",
        help="Skip the document-size-limit demonstration at the end.",
    )
    return parser.parse_args()


def doc_size_bytes(doc):
    return len(BSON.encode(doc))


def timed_push(coll, doc_id):
    reading = dict(READING, ts=time.time())
    start = time.perf_counter()
    coll.update_one({"_id": doc_id}, {"$push": {"readings": reading}})
    return time.perf_counter() - start


def measure_seeded_docs(coll, table):
    docs = list(coll.find({}).sort("_id", 1))
    if not docs:
        console.print("[red]lab_devices is empty -- run `python provision_lab.py --reset` first.[/]")
        return
    for doc in docs:
        existing = len(doc.get("readings", []))
        size_kb = round(doc_size_bytes(doc) / 1024, 1)
        latency_ms = round(timed_push(coll, doc["_id"]) * 1000, 1)
        table.add_row(doc["_id"], str(existing), str(size_kb), str(latency_ms))


def measure_test_size(db, size, table):
    scratch_id = f"device-test-{size}"
    scratch_coll = db["lab_devices_scratch"]
    readings = [dict(READING, ts=i) for i in range(size)]
    scratch_coll.replace_one({"_id": scratch_id}, {"_id": scratch_id, "readings": readings}, upsert=True)
    doc = scratch_coll.find_one({"_id": scratch_id})
    size_kb = round(doc_size_bytes(doc) / 1024, 1)
    latency_ms = round(timed_push(scratch_coll, scratch_id) * 1000, 1)
    table.add_row(scratch_id, str(size), str(size_kb), str(latency_ms))
    scratch_coll.delete_one({"_id": scratch_id})


def run_overflow_demo(db):
    console.print("[bold]Building a scratch document with a very large readings array...[/]")
    scratch_id = "device-overflow-scratch"
    scratch_coll = db["lab_devices_scratch"]
    scratch_coll.drop()

    # ~150 bytes/reading; ~120k readings lands just past the ~16 MB ceiling.
    big_reading_count = 120_000
    big_readings = [dict(READING, ts=i, padding="x" * 60) for i in range(big_reading_count)]
    overflow_doc = {"_id": scratch_id, "readings": big_readings}
    overflow_size_mb = round(doc_size_bytes(overflow_doc) / (1024 * 1024), 2)
    console.print(f"Attempting to insert 1 document with {big_reading_count:,} readings "
                  f"(~{overflow_size_mb} MB)...")

    try:
        scratch_coll.insert_one(overflow_doc)
    except (DocumentTooLarge, OperationFailure) as e:
        console.print(f"[red]Failed: {e}[/]")
    else:
        console.print(f"[yellow]Insert succeeded at ~{overflow_size_mb} MB -- increase "
                      "big_reading_count to push past the actual ceiling.[/]")
    finally:
        scratch_coll.drop()


def main():
    args = parse_args()
    console.print("[bold cyan]Lab 2: lab_devices[/]\n")
    db = get_lab_db()
    coll = db[LAB_COLLECTIONS["devices"]]

    if args.test_sizes:
        console.print(f"[dim]Collection: {db.name}.lab_devices_scratch (scratch, cleaned up after each size)[/]\n")
    else:
        console.print(f"[dim]Collection: {db.name}.{coll.name}"
                      + ("" if args.skip_overflow else f" (plus {db.name}.lab_devices_scratch for the overflow demo)")
                      + "[/]\n")

    table = Table(title="Document size and append latency vs. existing array length")
    table.add_column("_id")
    table.add_column("Existing readings", justify="right")
    table.add_column("Doc size (KB)", justify="right")
    table.add_column("$push latency (ms)", justify="right")

    if args.test_sizes:
        sizes = [int(s.strip()) for s in args.test_sizes.split(",") if s.strip()]
        for size in sizes:
            measure_test_size(db, size, table)
        console.print(table)
        return

    measure_seeded_docs(coll, table)
    console.print(table)

    if not args.skip_overflow:
        console.print()
        run_overflow_demo(db)


if __name__ == "__main__":
    main()
