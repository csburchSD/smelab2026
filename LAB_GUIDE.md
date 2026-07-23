# Firestore Anti-Patterns Lab — Lab Guide

You're working against a live Firestore Enterprise database (MongoDB-compatible
API) that has been deliberately seeded with **four schema/access-pattern
anti-patterns**, plus **three more scripts** that reproduce traffic- and
write-shape anti-patterns live rather than from seeded data. Each one causes
a real, measurable problem — slow queries, degrading write latency,
contention, or ballooning cost.

Your task: for each anti-pattern, use an AI assistant (and the tools below) to
1. **diagnose** what's wrong and why it happens on this database engine, and
2. **propose and implement a fix** — a schema change, an index, a different
   write pattern, etc.

No answer key is included here on purpose — that's the exercise.

## Connecting

**Prerequisites:** Python 3.9+. Everything else below sets that up into an
isolated environment so it can't collide with anything else on the machine.

```bash
cd ~/firestore/lab

# A venv is a private, disposable copy of Python's package directory, scoped
# to this shell. If one doesn't already exist here, create it:
test -d ~/venv || python3 -m venv ~/venv

# "Activate" points `python`/`pip` in your current shell at that private
# copy instead of the system Python -- run this every new shell/session:
source ~/venv/bin/activate

# Installs the three packages the lab scripts import (pymongo for the
# database driver, rich for the tables/output, python-dotenv to read .env):
pip install -r _internal/requirements.txt
```

The lab reads which database to connect to from a `.env` file in this
directory (`MONGO_URI=<connection string>`). Copy `.env.example` to `.env`
and fill in the connection string if one isn't already set up for you --
this contains a username/password, so it's gitignored and never committed:

```bash
cp -n .env.example .env  # -n: won't clobber a .env you already have; edit MONGO_URI inside if it's still a placeholder
```

Now confirm you can actually reach the database:

```bash
python provision_lab.py --check
```

This connects using `MONGO_URI` and reports what it finds -- something like
`Connected. Collections found: ['lab_bookings', 'lab_counters', 'lab_devices']`
means you're good to go. If instead it reports a connection error, `MONGO_URI`
in `.env` is wrong or the database/user isn't provisioned yet (see
`FACILITATOR_GUIDE.md`); if it connects but finds no collections, you're
pointed at the right host but the wrong (unseeded) database -- double check
the database name in `MONGO_URI`'s path (after the host, before the `?`).

## The seeded collections

| Collection | What's in it |
|---|---|
| `lab_counters` | One document (`_id: "global_stats"`) that gets incremented a lot |
| `lab_devices` | A handful of documents, each with a `readings` array of different sizes |
| `lab_events` | Starts empty — populated live when you write to it |
| `lab_bookings` | ~12,000 booking documents (`status`, `destination`, `price`, `created_at`, `customer_ref`) — also read (not modified) by script 6 |

Scripts 5 and 7 don't use any of the above — they create and drop their own
scratch collections each run. Each script prints which collection(s) it's
touching when it starts, so you always know what to go poke at directly.

## Reproducing each anti-pattern

Run these to see the symptom for yourself before (and after) you fix anything:

```bash
python 01_lab_counters.py   # $inc (atomic increment of a field) under concurrent load -- diagnose and fix the contention yourself
python 02_lab_devices.py       # $push (append to an array field) -- diagnose and fix the latency yourself
python 03_lab_events.py     # inserts into lab_events -- diagnose and fix the _id strategy yourself
python 04_inefficient_compound_fields.py  # explain() for a common query -- diagnose and fix the indexing yourself
python 05_lab_traffic_scratch.py         # a sudden concurrency spike vs. the same load ramped up in stages
python 06_large_reads_pagination.py       # fetching a whole collection at once vs. in pages
python 07_batch_vs_bulk_writes.py         # serial vs. batched (at a few sizes) vs. parallel individual writes
```

Use the worksheet below to track what you find before you jump to this
guide's AI-assistant prompts or `FACILITATOR_GUIDE.md` -- the goal is your
own working theory for each collection, not just a number in a table.

| # | Collection | Investigate | Your observation | Your proposed fix |
|---|---|---|---|---|
| 1 | `lab_counters` | Write latency vs. document count under the same total concurrent load | | |
| 2 | `lab_devices` | Document size / write latency vs. array length | | |
| 3 | `lab_events` | Insert latency vs. `_id` key strategy | | |
| 4 | `lab_bookings` | Query cost vs. index coverage (`explain()`) | | |
| 5 | `lab_traffic_scratch` | Latency vs. how traffic arrives (all at once vs. staged) | | |
| 6 | `lab_bookings` | Read cost/latency vs. fetch strategy | | |
| 7 | `lab_bulkwrite_scratch` | Write throughput vs. batch size / parallelism | | |

Each script prints the query or write pattern it's running — read the source,
not just the output. That's what you're being asked to fix.

## Testing your own hypothesis

Scripts 1-3 and 5-7 aren't just fixed before/after demos — each takes an
argument so you can point the same measurement harness at your own idea
instead of only the built-in scenarios:

```bash
python 01_lab_counters.py --docs N       # spread the same workload across N documents
python 02_lab_devices.py --test-sizes 50,500,5000   # measure specific array lengths
python 03_lab_events.py --shard-prefixes N       # a third key strategy, N possible prefixes
python 05_lab_traffic_scratch.py --mode reads                 # same spike-vs-ramp comparison, with reads
python 06_large_reads_pagination.py --pagination keyset         # a keyset cursor instead of skip/limit
python 06_large_reads_pagination.py --page-size N               # tune the page size
python 07_batch_vs_bulk_writes.py --batch-sizes 5,50,1000       # your own set of batch sizes
```

Run `--help` on any of them for details. These don't tell you what N (or
whether this is even the right knob) should be — that's still yours to
figure out and justify.

Useful things to ask your AI assistant about along the way -- notice these
ask you to explain *your own numbers*, not "what's the answer":
- "Here's what I measured for writes to one document vs. many -- what would
  explain a gap like this?"
- "Here's how this document's size and write latency changed as its array
  grew -- what's driving that, and where does it stop being viable?"
- "Here's the insert latency I measured across different `_id` key
  strategies -- what would explain the differences?"
- "What is the ESR (Equality, Sort, Range) rule for compound indexes, and does
  the index I just created follow it?"
- "Here's how the same total traffic behaved arriving all at once vs. ramped
  up gradually -- what would explain that?"
- "If fetching everything in one query isn't necessarily slower, what does
  pagination actually protect against?"
- "What's the difference between a batched write and a bunch of parallel
  individual writes, and when would you want one over the other?"

## Resetting

If you want to start over (undo your own changes, or hand the lab to the next
person):

```bash
python provision_lab.py --reset
```

This drops and rebuilds all four `lab_` collections, including any indexes you
added.

## Rules of engagement

- Stay inside the `lab_` collections — `customers`, `products`, `orders`, and
  `sensor_readings` belong to a different demo and aren't part of this
  exercise.
- `explain("executionStats")` on a `pymongo` cursor is your best diagnostic
  tool for anti-patterns 3 and 4. `db.command("collStats", "<name>")` is
  useful for 1 and 2.
