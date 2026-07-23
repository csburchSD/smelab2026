#!/usr/bin/env python3
"""Seed the Firestore Enterprise anti-patterns lab with four starting schemas.

Not meant to be run directly -- invoked via `provision_lab.py` (during
provisioning, or with --reset/--run-all). If you need it standalone, run
`python -m _internal.setup_lab` from the lab directory.

Idempotent: drops and rebuilds every lab_ collection, so this can be run
repeatedly across training sessions. Prefer `provision_lab.py --reset` as
the entry point between sessions -- it wraps this module and adds a clear
banner.

Collections seeded:
  1. lab_counters  -- a single document
  2. lab_devices   -- a handful of documents, each holding an array of a
                      different size
  3. lab_events    -- left empty; 03_lab_events.py writes into it live
  4. lab_bookings  -- a large collection

Labs 5-7 (05_lab_traffic_scratch.py, 06_large_reads_pagination.py,
07_batch_vs_bulk_writes.py) need no seeding here -- 5 and 7 create and drop
their own scratch collections, and 6 only reads lab_bookings above.
"""

import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.errors import OperationFailure
from rich.console import Console
from rich.panel import Panel

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()

BOOKING_STATUSES = ["pending", "confirmed", "completed", "cancelled"]
DESTINATIONS = ["Tokyo", "Lisbon", "Nairobi", "Toronto", "Auckland", "Reykjavik", "Marrakesh"]

# Array sizes chosen to stay well under the ~16 MB BSON document limit while
# still making the growth trend (size + append latency) obvious.
DEVICE_ARRAY_SIZES = [0, 200, 2000, 9000]

BOOKINGS_COUNT = 12000
BOOKINGS_BATCH = 1000


def create_index_with_retry(coll, keys, attempts=60, delay_s=10):
    """Dropping a collection here doesn't synchronously drop its indexes --
    there's an async teardown window, so recreating an index of the same
    shape right after coll.drop() can race against it (code 86,
    IndexAlreadyExists: "Index with matching definition is being
    deleted."). Measured directly against a live database: this window can
    take several minutes to clear (one observed run: ~372s), not the few
    seconds it might look like at a glance -- reset_lab.py hits this on
    every use, since it drops lab_bookings and immediately calls back into
    this function. Retry generously (default budget: 10 minutes) rather
    than fail the whole seed run over a timing window this backend is slow
    to clear."""
    for attempt in range(1, attempts + 1):
        try:
            coll.create_index(keys)
            return
        except OperationFailure as e:
            if e.code != 86 or attempt == attempts:
                raise
            if attempt == 1 or attempt % 6 == 0:
                console.print(
                    f"    index still tearing down from a previous run -- this backend can "
                    f"take several minutes here, waiting ({(attempt - 1) * delay_s}s elapsed, "
                    f"attempt {attempt}/{attempts})...", style="dim")
            time.sleep(delay_s)


def make_reading(i):
    return {
        "ts": datetime.now(timezone.utc) - timedelta(minutes=i),
        "value": round(random.uniform(15.0, 30.0), 2),
        "unit": "celsius",
        "quality": random.choice(["good", "good", "good", "degraded"]),
    }


def seed_counters(db):
    coll = db[LAB_COLLECTIONS["counters"]]
    coll.drop()
    coll.insert_one({"_id": "global_stats", "hits": 0, "last_updated": datetime.now(timezone.utc)})
    return 1


def seed_devices(db):
    coll = db[LAB_COLLECTIONS["devices"]]
    coll.drop()
    docs = []
    for size in DEVICE_ARRAY_SIZES:
        docs.append({
            "_id": f"device-{size:05d}-readings",
            "device_id": f"sensor-{size:05d}",
            "location": "Building A - Floor 1",
            "readings": [make_reading(i) for i in range(size)],
            "installed_at": datetime.now(timezone.utc) - timedelta(days=180),
        })
    coll.insert_many(docs)
    return len(docs)


def seed_events(db):
    coll = db[LAB_COLLECTIONS["events"]]
    coll.drop()
    coll.insert_one({"_id": "placeholder", "note": "populated live by 03_lab_events.py"})
    coll.delete_one({"_id": "placeholder"})
    return 0


def seed_bookings(db):
    coll = db[LAB_COLLECTIONS["bookings"]]
    coll.drop()
    inserted = 0
    for batch_start in range(0, BOOKINGS_COUNT, BOOKINGS_BATCH):
        batch = []
        for i in range(batch_start, min(batch_start + BOOKINGS_BATCH, BOOKINGS_COUNT)):
            batch.append({
                "customer_ref": str(uuid.uuid4()),
                "destination": random.choice(DESTINATIONS),
                "status": random.choices(BOOKING_STATUSES, weights=[15, 55, 25, 5])[0],
                "price": round(random.uniform(80.0, 2200.0), 2),
                "created_at": datetime.now(timezone.utc) - timedelta(days=random.uniform(0, 365)),
            })
        coll.insert_many(batch)
        inserted += len(batch)
        console.print(f"    ...{inserted}/{BOOKINGS_COUNT} lab_bookings seeded", style="dim")
    console.print("    building index on lab_bookings._id...", style="dim")
    create_index_with_retry(coll, [("_id", 1)])
    return inserted


def main():
    db = get_lab_db()

    console.print(Panel.fit(
        "[bold cyan]Firestore Anti-Patterns Lab -- Seeding[/]\n"
        f"Building 4 starting schemas into {db.name} (lab_ collections)",
        border_style="cyan",
    ))

    n = seed_counters(db)
    console.print(f"[bold]1.[/] lab_counters      -- {n} doc")

    n = seed_devices(db)
    console.print(f"[bold]2.[/] lab_devices       -- {n} docs, array sizes {DEVICE_ARRAY_SIZES}")

    n = seed_events(db)
    console.print("[bold]3.[/] lab_events        -- empty, ready for live inserts")

    n = seed_bookings(db)
    console.print(f"[bold]4.[/] lab_bookings      -- {n} docs")

    console.print(Panel.fit(
        "[bold green]Lab data ready.[/] Run scripts 01-04 in ~/firestore/lab/ to reproduce "
        "each anti-pattern's symptoms. Scripts 05-07 don't need seeded data -- they generate "
        "their own scratch collections live (05, 07) or read lab_bookings above (06).",
        border_style="green",
    ))


if __name__ == "__main__":
    main()
