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

**Run this lab from the same region as the database.** Cross-region calls
add real latency to every query, on top of whatever anti-pattern you're
trying to measure -- `provision_lab.py` will warn you if the two don't
match. To check which region this machine is in yourself (only works on a
GCE VM or Cloud Shell):

```bash
curl -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone
```

The response looks like `projects/123456789/zones/us-west1-b` -- the region
is everything before the last `-` (`us-west1` here).

## Connecting

**Prerequisites:** Python 3.9+, and a GCP project with billing linked (`gcloud
billing projects link YOUR_PROJECT_ID --billing-account=YOUR_BILLING_ACCOUNT_ID`
-- ask your facilitator for the billing account ID if you don't have it).
`provision_lab.py` below handles everything else, including enabling the
Firestore API itself -- billing is the one thing it can't do for you.

```bash
git clone https://github.com/csburchSD/smelab2026.git
cd smelab2026
```

```bash
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

Now provision your lab environment:

```bash
python provision_lab.py --project-id YOUR_PROJECT_ID
```

This creates a Firestore Enterprise database and user in your GCP project,
seeds it, and writes a working `.env` for you -- nothing to copy or edit by
hand.

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

## MongoDB compatibility notes

This is a real MongoDB-compatible API, not a MongoDB clone or Firestore
Native mode with different syntax — most things behave exactly like
community MongoDB. A few things don't, and they're exactly the kind of
gotcha worth knowing going in rather than losing time to mid-exercise.

**If you know MongoDB:**
- **`retryWrites=false` is required, not the default.** The driver never
  auto-retries a failed write — every write-issuing script in this lab
  catches the error itself instead of relying on the driver. (Lab 1)
- **No default `_id` index.** Real MongoDB auto-creates one on every
  collection; this backend doesn't — `coll.index_information()` returns
  `{}` until one is created. (Lab 6)
- **Every write is already an implicit transaction.** Concurrent writes to
  the same document use optimistic concurrency control (OCC) with snapshot
  isolation, not MongoDB's default pessimistic per-document locking —
  collisions are retried internally and, if they can't get a clean commit,
  hard-fail with `ABORTED`, rather than queuing behind a lock. (Lab 1)
- **`collMod: expireAfterSeconds` isn't supported.** To change a TTL
  index's retention window, drop and recreate the index with the new value
  — you can't modify one in place. (Lab 2)
- **Billing is per Read Unit** (data *scanned*, at 4 KiB granularity, not
  data *returned*), not a MongoDB Atlas-style compute/storage model. (Labs
  4 and 6)

**If you know Firestore's Native/Datastore mode instead:**
- **No subcollections.** A document nesting its own collection is a
  Native-mode-only concept — everything here is a flat, top-level
  collection. (Lab 2)
- **Auto-generated `_id` isn't fully random.** If you omit `_id` on insert,
  this driver assigns a BSON `ObjectId` (same as real MongoDB), not Native
  mode's fully-random auto-ID — an `ObjectId`'s leading 4 bytes are a Unix
  timestamp, so IDs inserted in the same second still share a prefix. (Lab 3)

None of this needs memorizing up front — it's here so that if something
behaves unexpectedly, you can check this list before assuming your own code
is wrong.

## Reproducing each anti-pattern

Run each base command to see the symptom for yourself before (and after) you
fix anything -- for all of them, diagnose and document what's happening and
what would fix it. Most also take an argument so you can point the same
measurement harness at your own idea instead of only the built-in scenario
(run `--help` on any of them for the full set); these don't tell you what N
(or whether it's even the right knob) should be, that's still yours to
figure out and justify.

| # | Scenario | Base command | Optional |
|---|---|---|---|
| 1 | `$inc` (atomic increment of a field) under concurrent load | `python 01_lab_counters.py` | `--docs N` -- spread the same workload across N documents |
| 2 | `$push` (append to an array field) under concurrent load | `python 02_lab_devices.py` | `--test-sizes 50,500,5000` -- measure specific array lengths |
| 3 | Inserts into `lab_events` across three `_id` key strategies | `python 03_lab_events.py` | `--shard-prefixes N` -- try a different prefix count for the third strategy (0 disables it); `--ops-per-writer N` -- sustain load over a longer window if a quick burst doesn't show a gap |
| 4 | `explain()` for a common query | `python 04_inefficient_compound_fields.py` | — |
| 5 | Sudden concurrency spike vs. the same load ramped up in stages | `python 05_lab_traffic_scratch.py` | `--mode reads` -- same spike-vs-ramp comparison, with reads |
| 6 | Fetching a whole collection at once vs. in pages | `python 06_large_reads_pagination.py` | `--pagination keyset` -- keyset (cursor-based) pagination instead of skip/limit: seeks from the last-seen `_id` instead of skip(n) walking and discarding n entries every page; `--page-size N` -- tunes the page size for either |
| 7 | Serial vs. batched (at a few sizes) vs. parallel individual writes | `python 07_batch_vs_bulk_writes.py` | `--batch-sizes 5,50,1000` -- your own set of batch sizes |

Use the worksheet below to track what you find before you jump to this
guide's AI-assistant prompts or `FACILITATOR_GUIDE.md` -- the goal is your
own working theory for each collection, not just a number in a table.

| # | Collection | Investigate | Your observation | Your proposed fix |
|---|---|---|---|---|
| 1 | `lab_counters` | Write latency vs. document count under the same total concurrent load | | |
| 2 | `lab_devices` | Document size / write latency vs. array length | | |
| 3 | `lab_events` | Insert latency (and latency drift across a run) vs. `_id` key strategy | | |
| 4 | `lab_bookings` | Query cost vs. index coverage (`explain()`) | | |
| 5 | `lab_traffic_scratch` | Latency vs. how traffic arrives (all at once vs. staged) | | |
| 6 | `lab_bookings` | Read cost/latency vs. fetch strategy | | |
| 7 | `lab_bulkwrite_scratch` | Write throughput vs. batch size / parallelism | | |

Each script prints the query or write pattern it's running — read the source,
not just the output. That's what you're being asked to document.

## Ask your AI assistant

A few prompts worth trying along the way -- notice these ask you to explain
*your own numbers*, not "what's the answer":
- "Here's what I measured for writes to one document vs. many -- what would
  explain a gap like this?"
- "Here's how this document's size and write latency changed as its array
  grew -- what's driving that, and where does it stop being viable?"
- "Here's the insert latency (and each scenario's latency drift across its
  own run) I measured across different `_id` key strategies -- what would
  explain the differences, and why might a monotonic key still be risky in
  production even if my numbers here don't show a clean gap?"
- "What is the ESR (Equality, Sort, Range) rule for compound indexes, and does
  the index I just created follow it?"
- "Here's how the same total traffic behaved arriving all at once vs. ramped
  up gradually -- what would explain that?"
- "If fetching everything in one query isn't necessarily slower, what does
  pagination actually protect against?"
- "What's the difference between a batched write and a bunch of parallel
  individual writes, and when would you want one over the other?"

## Reference: two patterns worth understanding (read after you've tried labs 1 and 2)

This section names two patterns directly, on purpose — not to skip the
exercise, but because a pre-lab survey found these were the two
lowest-confidence topics on the team, even after doing the lab. Try labs 1
and 2 yourself first; come back here to check your own fix against the
general pattern, or if you're stuck on the concept itself rather than just
this lab's specific numbers.

**Sharded (distributed) counter, for a hot document under concurrent writes
(lab 1):** instead of one document taking every increment, maintain N
separate "shard" documents (e.g. `global_stats_shard_0` through
`global_stats_shard_9`). Each write picks a random shard and increments that
one instead of a single shared document — spreading the same total write
volume across N independent documents that don't contend with each other.
Reading the total means reading all N shards and summing them, which is the
real trade-off: you've turned one cheap read into N reads to get the same
number. More shards means less write contention but a more expensive read;
the right N depends on your actual write rate, not a fixed rule. This is the
standard fix for any single "hot" document under concurrent writes, not
just counters — the same idea applies to any field every request needs to
update.

**Bucket pattern, for an unbounded array embedded in one document (lab
2):** instead of appending forever to one array field in one document,
split the growing data across multiple documents — one per time window
(e.g. per day) or one per N items, whichever matches how the data is
actually read back. Each bucket document has a natural size ceiling by
construction, so no single document grows without bound, and each append is
a write to whichever bucket is currently open, not a rewrite of the whole
history. The trade-off is the same shape as sharded counters: reading "all
of a device's history" now means reading multiple bucket documents instead
of one, and you generally want to keep a small, bounded summary (the latest
value, a running total) in a separate always-cheap-to-read document rather
than paying that multi-document cost for the common case.

Both patterns share the same underlying trade-off: **you're trading one
cheap, unbounded-risk operation for several bounded, safer ones.** That
trade only pays off when the unbounded version was actually going to be a
problem — sharding a counter nobody writes to concurrently, or bucketing an
array that never grows past a few dozen items, just adds complexity for
nothing.

## Resetting

If you want to start over (undo your own changes, or hand the lab to the next
person):

```bash
python provision_lab.py --reset
```

This drops and rebuilds all four `lab_` collections, including any indexes you
added.

## Tearing down

When you're completely done with the lab (not just resetting the data, but
removing the database, its user, and the IAM access it granted):

```bash
python decommission_lab.py --project-id YOUR_PROJECT_ID
```

This looks up every SCRAM user on the database (not just the one in your
`.env`), removes each one's IAM data-access binding and credentials, then
deletes the Firestore Enterprise database itself -- which also removes every
`lab_` collection in it. It'll ask you to type the database ID back to
confirm before deleting anything (skip that with `--yes`), and this is not
reversible -- there's no undo for a deleted database.

If you just want to see what it would do first, without touching anything:

```bash
python decommission_lab.py --project-id YOUR_PROJECT_ID --dry-run
```
