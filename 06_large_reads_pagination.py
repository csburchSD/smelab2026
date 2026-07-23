#!/usr/bin/env python3
"""Anti-pattern 6: Large Reads That Return Many Documents.

lab_bookings (12,000 docs, seeded by setup_lab.py) is read here two ways:

  UNPAGINATED: one query, no limit -- the whole collection materialized
  client-side in a single call.
  PAGINATED (skip/limit): the same result set fetched in fixed-size pages
  via .skip(n).limit(page_size), one round trip per page.

Default (no arguments): runs both and reports, per scenario, the first
page's latency, the last page's latency, and the average -- watch how
those three numbers relate to each other as page count grows. It also
reports read units (via explain()) for each scenario's first and last
page -- a serverless database bills per unit of backend work a query
does, not per wall-clock second, so it's worth comparing independently
rather than assuming it moves the same way latency does.

Your turn: skip/limit isn't the only way to paginate. Test an alternative
cursor strategy against the same data:

    python 06_large_reads_pagination.py --pagination keyset

which adds a third row using keyset (cursor-based) pagination: instead of
skip(n) walking and discarding n entries every page, seek directly to
where the last page left off using the last-seen _id as the start of an
`_id`-based range query (`{_id: {$gt: last_id}}`) instead of `.skip()`.
Compare its first-page/last-page numbers -- latency and read units both --
to skip/limit's and to the unpaginated row above.

    python 06_large_reads_pagination.py --page-size 100

changes the page size for both paginated scenarios.
"""

import argparse
import statistics
import time

from rich.console import Console
from rich.table import Table

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--page-size", type=int, default=500,
        help="Page size for the paginated scenarios (default: 500).",
    )
    parser.add_argument(
        "--pagination", choices=["keyset"], default=None,
        help="Add a keyset-cursor pagination scenario alongside skip/limit -- seeks "
             "directly from the last-seen _id instead of skip(n) walking and "
             "discarding n entries every page.",
    )
    return parser.parse_args()


def read_units(cursor):
    return cursor.explain()["executionStats"].get("readUnits", "?")


def fetch_unpaginated(coll):
    start = time.perf_counter()
    total = len(list(coll.find({})))
    wall = time.perf_counter() - start
    ru = read_units(coll.find({}))
    return total, 1, [wall * 1000], wall, {"first_ru": ru, "last_ru": ru}


def fetch_paginated_skip(coll, page_size):
    start = time.perf_counter()
    total = 0
    pages = 0
    page_latencies = []
    skip = 0
    last_page_skip = 0
    while True:
        page_start = time.perf_counter()
        page = list(coll.find({}).sort("_id", 1).skip(skip).limit(page_size))
        page_latencies.append((time.perf_counter() - page_start) * 1000)
        if not page:
            break
        total += len(page)
        pages += 1
        last_page_skip = skip
        skip += page_size
    wall = time.perf_counter() - start

    def cursor_at(s):
        return coll.find({}).sort("_id", 1).skip(s).limit(page_size)

    cost = {"first_ru": read_units(cursor_at(0)), "last_ru": read_units(cursor_at(last_page_skip))}
    return total, pages, page_latencies, wall, cost


def fetch_paginated_keyset(coll, page_size):
    start = time.perf_counter()
    total = 0
    pages = 0
    page_latencies = []
    last_id = None
    last_page_cursor_id = None
    while True:
        query = {} if last_id is None else {"_id": {"$gt": last_id}}
        page_start = time.perf_counter()
        page = list(coll.find(query).sort("_id", 1).limit(page_size))
        page_latencies.append((time.perf_counter() - page_start) * 1000)
        if not page:
            break
        total += len(page)
        pages += 1
        last_page_cursor_id = last_id
        last_id = page[-1]["_id"]
    wall = time.perf_counter() - start

    def cursor_at(cursor_id):
        query = {} if cursor_id is None else {"_id": {"$gt": cursor_id}}
        return coll.find(query).sort("_id", 1).limit(page_size)

    cost = {"first_ru": read_units(cursor_at(None)), "last_ru": read_units(cursor_at(last_page_cursor_id))}
    return total, pages, page_latencies, wall, cost


def summarize(name, total, pages, page_latencies, wall, cost):
    return {
        "scenario": name,
        "pages": pages,
        "total_docs": total,
        "wall_s": round(wall, 3),
        "first_page_ms": round(page_latencies[0], 1),
        "last_page_ms": round(page_latencies[-1], 1),
        "avg_page_ms": round(statistics.mean(page_latencies), 1),
        "first_ru": cost["first_ru"],
        "last_ru": cost["last_ru"],
    }


def print_table(rows):
    table = Table(title="Fetching all of lab_bookings: unpaginated vs. paginated")
    table.add_column("Scenario")
    table.add_column("Pages", justify="right")
    table.add_column("Total docs", justify="right")
    table.add_column("Wall (s)", justify="right")
    table.add_column("First page (ms)", justify="right")
    table.add_column("Last page (ms)", justify="right")
    table.add_column("Avg page (ms)", justify="right")
    table.add_column("Read units (1st pg)", justify="right")
    table.add_column("Read units (last pg)", justify="right")

    for row in rows:
        table.add_row(row["scenario"], str(row["pages"]), str(row["total_docs"]), str(row["wall_s"]),
                      str(row["first_page_ms"]), str(row["last_page_ms"]), str(row["avg_page_ms"]),
                      str(row["first_ru"]), str(row["last_ru"]))
    console.print(table)


def main():
    args = parse_args()
    console.print("[bold cyan]Anti-Pattern 6: Large Reads That Return Many Documents[/]\n")
    db = get_lab_db()
    coll = db[LAB_COLLECTIONS["bookings"]]
    console.print(f"[dim]Collection: {db.name}.{coll.name}[/]\n")

    console.print("Fetching the whole collection in one unpaginated query...")
    rows = [summarize("UNPAGINATED", *fetch_unpaginated(coll))]

    console.print(f"Fetching the same data in pages of {args.page_size} via skip/limit...")
    rows.append(summarize(f"PAGINATED skip/limit ({args.page_size}/page)",
                           *fetch_paginated_skip(coll, args.page_size)))

    if args.pagination == "keyset":
        console.print(f"Fetching the same data in pages of {args.page_size} via a keyset cursor...\n")
        rows.append(summarize(f"PAGINATED keyset ({args.page_size}/page)",
                               *fetch_paginated_keyset(coll, args.page_size)))
    else:
        console.print()

    print_table(rows)
    console.print(
        "\n[dim]Watch first-page vs. last-page latency within a scenario, not just the totals -- "
        "that's where a pagination strategy's behavior as page count grows shows up. Read units "
        "are a separate, backend-cost axis -- don't assume they track latency the same way; "
        "compare them across all three rows, including the unpaginated one, on their own terms.[/]"
    )


if __name__ == "__main__":
    main()
