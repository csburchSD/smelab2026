#!/usr/bin/env python3
"""Anti-pattern 4: Inefficient Compound Fields.

lab_bookings (12,000 docs) is seeded with no indexes beyond `_id`. This
script runs one ordinary query against it -- an equality filter, a range
filter, and a sort, the kind of "search bookings" query a real app would
make -- and prints its explain("executionStats") output:

    find({status: "confirmed", price: {$gte, $lte}}).sort("created_at", -1).limit(20)

It does not build or compare any index for you. Diagnose why this query
costs what it does from the explain() output (docs examined vs. returned,
the stage path, and read units -- the billed cost of running it), then
design and create your own index. Re-run this script afterward -- against
whatever indexing you've added -- to see whether it actually helped, both
in latency and in read units.
"""

from rich.console import Console
from rich.table import Table

from _internal.lab_config import LAB_COLLECTIONS, get_lab_db

console = Console()

QUERY_FILTER = {"status": "confirmed", "price": {"$gte": 200, "$lte": 800}}
SORT = [("created_at", -1)]
LIMIT = 20


def run_explain(coll):
    cursor = coll.find(QUERY_FILTER).sort(SORT).limit(LIMIT)
    return cursor.explain()


def _mongosh_literal(value):
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_mongosh_literal(v)}" for k, v in value.items()) + "}"
    return str(value)


def format_query(coll_name):
    """Render QUERY_FILTER/SORT/LIMIT as a standalone mongosh statement --
    unlike Python's dict repr (single-quoted, and ".sort(created_at DESC)"
    was never real syntax to begin with), this is directly runnable in a
    mongosh shell/`--eval`. Includes the `db.<collection>.` prefix mongosh
    needs (there's no bound `coll` variable there the way there is in this
    script), and uses only double-quoted string literals so it survives
    being pasted into a single-quoted `--eval '...'` shell argument without
    any escaping."""
    filt = _mongosh_literal(QUERY_FILTER)
    sort_obj = "{" + ", ".join(f"{field}: {direction}" for field, direction in SORT) + "}"
    return f"db.{coll_name}.find({filt}).sort({sort_obj}).limit({LIMIT})"


def stage_names(stage, acc=None):
    acc = acc if acc is not None else []
    if isinstance(stage, dict):
        if "stage" in stage:
            acc.append(stage["stage"])
        if "inputStage" in stage:
            stage_names(stage["inputStage"], acc)
    return acc


def main():
    console.print("[bold cyan]Anti-Pattern 4: Inefficient Compound Fields[/]\n")
    console.print(f"Query: {format_query(LAB_COLLECTIONS['bookings'])}\n")

    db = get_lab_db()
    coll = db[LAB_COLLECTIONS["bookings"]]
    console.print(f"[dim]Collection: {db.name}.{coll.name}[/]\n")

    plan = run_explain(coll)
    es = plan["executionStats"]
    stages = stage_names(es.get("executionStages", {}))

    table = Table(title='explain("executionStats") for the query above')
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Docs examined", str(es.get("totalDocsExamined", "?")))
    table.add_row("Keys examined", str(es.get("totalKeysExamined", "-")))
    table.add_row("Returned", str(es["nReturned"]))
    table.add_row("Read units", str(es.get("readUnits", "?")))
    table.add_row("Time (ms)", str(es["executionTimeMillis"]))
    table.add_row("Stage path", " -> ".join(stages))

    console.print(table)
    console.print(
        "\n[dim]Read units is what this query is actually billed for -- a serverless "
        "database charges per unit of work a query does, not per query, so a docs-examined "
        "count far above what's returned costs real money on every single call, not just wall "
        "time. `db.lab_bookings.count()` is a useful reference point for that. When you design "
        "your index, the ESR rule -- Equality fields, then Sort fields, then Range fields, in "
        "that order -- is a good guide for field order. Paste this into `mongosh` or Firestore "
        "Studio's query runner for a closer look:"
        f"\n\n  {format_query(coll.name)}[/]"
    )
    console.print(
        "\n[bold]Next:[/] build your index in Firestore Studio -- it shows build progress "
        "live, so you'll know when it's actually ready instead of guessing -- then re-run "
        "this script and compare the table above."
    )


if __name__ == "__main__":
    main()
