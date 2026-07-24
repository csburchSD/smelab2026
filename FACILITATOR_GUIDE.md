# Firestore Anti-Patterns Lab — Facilitator Guide

Setup, architecture, and the answer key, all in one place. Don't hand this
to trainees — give them `LAB_GUIDE.md` instead; it has no fixes given away.
Symptom numbers below were measured against a live database
on 2026-07-14 (or `labdb1`/other databases where noted for labs added
later); re-running will vary but the relative shape should hold.

## Architecture, in one paragraph

Each Firestore Enterprise database is its **own MongoDB-compatible
endpoint** — a distinct host keyed by database UID + region
(`<uid>.<location>.firestore.goog:443`), with its own SCRAM users. It is
*not* a database name selectable on a shared cluster the way plain MongoDB
multi-tenancy works. Because of that, this lab's connection info lives
entirely in one `.env` file as a single `MONGO_URI` connection string
(`_internal/lab_config.py`) — pointing the lab at a different database means
generating a new connection string for that database, not just changing a
name.

## File structure

`provision_lab.py` is the single entry point for everything trainee-facing
-- provisioning, resetting, and checking connectivity are all flags/menu
options on it (`--reset`, `--check`, `--run-all`; see "Fast path" below).
Everything under `_internal/` is plumbing a trainee never needs to know by
name.

| File | Purpose |
|---|---|
| `provision_lab.py` | Creates a Firestore Enterprise database + SCRAM user + IAM binding from a GCP project ID, writes `.env`, then seeds it. Also the entry point for `--reset`, `--check`, and `--run-all` (facilitator-only). The fast path — see below. |
| `decommission_lab.py` | Tears down everything `provision_lab.py` created, including deleting the database. Destructive, not reversible. |
| `_internal/lab_config.py` | Loads `.env`, exposes `get_lab_db()` and `LAB_COLLECTIONS` |
| `.env` / `.env.example` | `MONGO_URI=<connection string>` — the only thing that changes per environment |
| `_internal/requirements.txt` | Pinned deps (`pip install -r _internal/requirements.txt`) |
| `_internal/setup_lab.py` | Seeds the 4 persistent broken schemas (idempotent); invoked via `provision_lab.py`, not run directly |
| `_internal/reset_lab.py` | Wraps `setup_lab.py` with a clear "wipe + reseed" banner; invoked via `provision_lab.py --reset` |
| `_internal/run_all.py` | Resets, then runs all 7 anti-pattern scripts in sequence; invoked via `provision_lab.py --run-all` |
| `01`–`07_*.py` | The anti-pattern scripts themselves (see table below) |
| `LAB_GUIDE.md` | Trainee-facing exercise guide — no fixes given away |
| `FACILITATOR_GUIDE.md` | This file |

## The seven anti-patterns (facilitator reference — full answers below)

| # | Script | Collection | Demonstrates |
|---|---|---|---|
| 1 | `01_lab_counters.py` | `lab_counters` | Concurrent writes to one hot document vs. spread across many |
| 2 | `02_lab_devices.py` | `lab_devices` | Document size / write latency growing with an embedded array, up to the ~16MB ceiling |
| 3 | `03_lab_events.py` | `lab_events` | Monotonic vs. random insert keys |
| 4 | `04_inefficient_compound_fields.py` | `lab_bookings` | `explain()` on a query with no supporting compound index |
| 5 | `05_lab_traffic_scratch.py` | `lab_traffic_scratch` (script-managed) | Sudden concurrency spike vs. the same load ramped up in stages, against a narrow doc range |
| 6 | `06_large_reads_pagination.py` | `lab_bookings` (read-only) | Unpaginated vs. paginated (skip/limit, keyset) fetch of a large result set |
| 7 | `07_batch_vs_bulk_writes.py` | `lab_bulkwrite_scratch` (script-managed) | Serial vs. batched (at a few sizes) vs. parallel-individual ("bulk") writes |

Only 1–4 need seeded data (`setup_lab.py`); 5 and 7 create and drop their
own scratch collections per run, and 6 only reads the collection lab 4
already seeded. "Script-managed" means the *collection*'s lifecycle is
handled by the script, not by `setup_lab.py` — every lab, 1 through 7, runs
against the same serverless Firestore Enterprise database named in
`MONGO_URI`. None of them touch a self-hosted MongoDB cluster.

Scripts 1-3 and 5-7 also accept a flag (`--docs`, `--test-sizes`,
`--shard-prefixes`/`--ops-per-writer`, `--mode`, `--pagination`/`--page-size`,
`--batch-sizes` respectively) that turns them into a harness for testing a
trainee's own hypothesis instead of just the built-in comparison — see each
section below for how to use it as a nudge without giving away the fix.

## Setting up a new environment

### 1. Prerequisites

- A GCP project with billing linked (`gcloud billing projects link
  YOUR_PROJECT_ID --billing-account=YOUR_BILLING_ACCOUNT_ID`) --
  `provision_lab.py` doesn't check or fix this itself, so if it's missing
  you'll see a raw `gcloud` billing-not-configured error partway through.
  (The Firestore API itself doesn't need enabling by hand: `provision_lab.py`
  calls `gcloud services enable firestore.googleapis.com` on every run,
  before it tries to create anything -- confirmed necessary live, on a
  brand-new project where `gcloud`'s own "enable it now?" prompt turned out
  to default to declining even under `--quiet`.)
- `gcloud` CLI, authenticated (`gcloud auth login`) against that project,
  with **more than just a Firestore-scoped role**. `provision_lab.py` also
  calls `gcloud projects add-iam-policy-binding` to grant the new SCRAM user
  access -- that's a project-level IAM-policy-modification permission
  (`resourcemanager.projects.setIamPolicy`), which a Firestore-only role
  (even `roles/datastore.owner`) does not include. Owner, Editor, or Project
  IAM Admin (paired with a Firestore admin role) all work.
- Python 3.9+.

### 2. Get the code and set up Python

```bash
git clone https://github.com/csburchSD/smelab2026.git
cd smelab2026
```

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r _internal/requirements.txt
```

### 3. Fast path: provision_lab.py

```bash
python provision_lab.py --project-id YOUR_PROJECT_ID
```

This creates the database, the SCRAM user, the IAM binding, waits for the
binding to propagate, writes `.env`, and seeds the lab data — all of steps
4–8 below, in one command. It prints the plan and asks for confirmation
before creating anything (real, billed resources), and never prints the
generated password. Use `--dry-run` to see the plan without touching
anything, or `--database-id`/`--location`/`--user-id` to override the
defaults. Run `python provision_lab.py --help` for details.

**Region matters here — same-region scripts and database is not optional.**
If `--location` isn't given, `provision_lab.py` tries the GCE metadata
server to detect the region this machine is actually running in and uses
that as the default instead of a hardcoded location; it falls back to
`us-west1` only if metadata isn't reachable (e.g. a trainee's own laptop,
not a GCE VM). Either way, if the machine's detected region and the
database's region don't match — an explicit `--location` override, or
reusing an existing database created elsewhere — it prints a warning
explaining the cross-region latency cost and folds a confirmation into the
usual resource-creation prompt before proceeding. `--check` (including via
the interactive menu's "Check connection" option) runs the same comparison
against whatever `.env` already points at, so a trainee who inherited
someone else's `.env` gets warned too, not just at provisioning time.

Expect it to pause for a bit in two places -- this is normal, not a hang:
waiting up to ~2 minutes for the new IAM binding to propagate before it'll
confirm write access, and (if reusing a `--database-id` that was decommissioned
moments earlier) waiting out gcloud's reuse cooldown before it'll let you
recreate a database under the same ID.

If propagation takes longer than that ~2-minute budget (observed directly:
it can), the script gives up waiting, skips seeding, and tells you to run
`python provision_lab.py --reset` yourself once access is confirmed -- so
"one command" above is the common case, not a guarantee. `.env` is already
written at that point either way.

Skip to [Verifying it worked](#verifying-it-worked) once it finishes — or
read on for what it's actually doing, useful if something needs manual
troubleshooting.

### 4. Manual path: create a Firestore Enterprise database with MongoDB compatibility

```bash
gcloud firestore databases create \
  --project=YOUR_PROJECT_ID \
  --database=YOUR_DATABASE_ID \
  --location=YOUR_LOCATION \
  --edition=enterprise \
  --enable-mongodb-compatible-data-access
```

(`YOUR_LOCATION` is a Firestore location such as `us-west1` — see
`gcloud firestore databases create --help` for the full list. Pick one that
matches the region the lab scripts will actually run from; the manual path
has no equivalent of the fast path's region auto-detection/mismatch
warning, so a wrong choice here fails silently as elevated latency, not an
error.)

### 5. Create a SCRAM user and grant it IAM data access

```bash
gcloud firestore user-creds create YOUR_USER \
  --database=YOUR_DATABASE_ID --project=YOUR_PROJECT_ID
# Prints a password ONCE -- save it, it can't be retrieved again.

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="principal://firestore.googleapis.com/projects/YOUR_PROJECT_NUMBER/name/databases/YOUR_DATABASE_ID/userCreds/YOUR_USER" \
  --role=roles/datastore.owner \
  --condition='expression=resource.name == "projects/YOUR_PROJECT_ID/databases/YOUR_DATABASE_ID",title=lab-user-access'
```

`YOUR_PROJECT_NUMBER` (not the project ID) is in `gcloud projects describe
YOUR_PROJECT_ID --format='value(projectNumber)'`.

**`roles/datastore.owner`, not `roles/datastore.user`:** every lab script
that uses a scratch collection (and `setup_lab.py` itself) calls
`coll.drop()`. `roles/datastore.user` (23 permissions) doesn't cover
collection/index administration and `drop()` fails with `PermissionDenied`
no matter how long you wait for propagation — confirmed directly with
`gcloud iam roles describe roles/datastore.user`, and by comparing against
a role that has 63 permissions and works. `roles/datastore.owner` is what
this lab actually needs.

**Gotcha hit while building this lab:** a SCRAM user can authenticate
successfully immediately, but every read/write fails with
`PermissionDenied` until the IAM binding above finishes propagating — this
took a couple of minutes in practice. Don't assume the binding command
failed if the very next write attempt still 403s; retry after a short wait.

### 6. Build the connection string

```bash
gcloud firestore databases describe --database=YOUR_DATABASE_ID \
  --project=YOUR_PROJECT_ID --format='value(uid,locationId)'
```

Then:

```
mongodb://YOUR_USER:YOUR_PASSWORD@<uid-from-above>.<location-from-above>.firestore.goog:443/YOUR_DATABASE_ID?loadBalanced=true&tls=true&authMechanism=SCRAM-SHA-256&retryWrites=false
```

`retryWrites=false` is required on this connection — the driver won't
auto-retry a failed write, which is why every script that writes catches
`OperationFailure` itself (see `01_lab_counters.py`'s `timed_increment`
for the pattern).

### 7. Configure `.env`

```bash
cp .env.example .env
# edit .env: MONGO_URI=<the connection string from step 6>
```

### 8. Seed and run

```bash
python provision_lab.py --reset   # seed the 4 persistent broken schemas
python provision_lab.py --run-all # or run any script individually, e.g.:
python 01_lab_counters.py
```

Between sessions (or to hand the lab to the next person), reseed with the
same `--reset` command — dropping and reseeding a collection that already
exists is exactly what "start fresh" needs, whether it's the first time or
the tenth:

```bash
python provision_lab.py --reset
```

This drops and rebuilds all four `lab_` collections, including any indexes
a trainee added.

When the whole database is no longer needed (e.g. after a workshop), tear
it down completely — this deletes the database and everything in it, and
is not reversible:

```bash
python decommission_lab.py --project-id YOUR_PROJECT_ID --dry-run   # see the plan first
python decommission_lab.py --project-id YOUR_PROJECT_ID
```

It defaults `--database-id` from `.env`'s `MONGO_URI`, so if that file is
still the one `provision_lab.py` wrote, `--project-id` is the only flag you
need — it discovers and cleans up every SCRAM user actually on the
database live (via `gcloud firestore user-creds list`), not just the one
`.env` remembers, so nothing gets silently orphaned if `.env` is missing
or stale.

## Verifying it worked

```bash
python provision_lab.py --check
```

should report the four seeded `lab_` collections with no errors. It also
compares the database's region (parsed out of `.env`'s `MONGO_URI`) against
the region this machine is detected to be running in, and warns if they
don't match — see the region note under "Fast path" above.

---

## 1. Single Document Contention — `lab_counters`

**Setup:** one document, `_id: "global_stats"`, incremented via `$inc`
(the operator that atomically increments a numeric field) by many
concurrent callers (e.g. a naive global page-view counter).

**Symptom (`01_lab_counters.py`):** the script now reports only the median
(p50) latency per scenario, on purpose — it used to also show wall-clock,
max, and ops/sec, but those turned out to be routinely dominated by a single
outlier op unrelated to contention (see caveat below), which buried the
actual signal. Median still shows it clearly: 20 concurrent writers doing
100 total `$inc` ops against one shared document run materially slower than
the same 100 ops spread across 20 documents (measured: ~95ms hot vs. ~14ms
cold). Firestore serializes writes to a single document; concurrent writers
queue behind each other.

**Caveat worth knowing before a trainee asks about it:** the COLD scenario
reproducibly shows one very slow op (~5.0-5.1s, essentially the same value
across repeated runs, not random noise) that used to dominate the old
wall-clock/max columns while never showing up as an error (writes still
succeed) or moving the median. Leading theory, not yet fully confirmed:
`run_spread()` always executes before `run_hot()` in a fresh process, so
whichever scenario runs first likely eats a one-time pymongo connection-pool
growth cost (the client going from a handful of sockets up to
`WRITERS`-many concurrent ones for the first time) — that would land on
COLD by construction, regardless of anything about contention. If a trainee
reports this, it's a legitimate observation worth discussing, not a sign
their measurement is broken; median is what the lab's actual comparison
rests on, precisely because it isn't sensitive to this kind of one-off.

**Fix:** shard the counter. Maintain N separate shard documents (e.g.
`global_stats_shard_0..9`), write to a randomly chosen shard per increment,
and sum shards to read the total. (These are separate top-level documents in
the collection, not sub-documents/embedded documents — this MongoDB-compatible
edition of Firestore has no subcollection or parent/child document hierarchy
the way Firestore's native mode does.) This is the standard "distributed
counter" pattern (same idea Firestore's own docs recommend for native mode). For
non-counter hot documents (e.g. a single "settings" doc every request reads
*and* writes), the fix is usually to split read-heavy and write-heavy fields
into separate documents instead.

**Trap to watch for:** trainees may reach for an explicit transaction or a
retry loop — that reduces *correctness* risk (lost updates) but does not fix
contention; it can make tail latency worse by adding retries on conflict.
Worth being precise about *why*: every individual write in Firestore
Enterprise (a plain `update_one`, not just an explicit multi-document
transaction) already runs as an implicit transaction using optimistic
concurrency control with snapshot isolation, not MongoDB's default
pessimistic per-document locking ([behavior differences
doc](https://docs.cloud.google.com/firestore/mongodb-compatibility/docs/behavior-differences#transactions)).
Concurrent writers to the same document collide at commit time; the backend
retries internally, and if it can't get a clean commit it hard-fails with
`ABORTED: Too much contention on these documents`. Since `retryWrites=false`
is required on this connection, the driver never auto-retries that (or any
transient failure) — the app must. `01_lab_counters.py` now
catches and counts these instead of crashing on the first one, and calls
them out explicitly (only when they actually happen, not as a permanent
column) since that's a real result, not noise; at this lab's default
concurrency (20 writers × 5 ops) the backend's internal retries absorb the
contention and no errors surface — push `WRITERS`/`OPS_PER_WRITER` higher to
see the abort rate climb.

**Nudge, don't tell:** `--docs N` spreads the same total workload across N
documents (N=1 reproduces HOT, N=20 reproduces COLD) without the script
saying why that helps or how to reconstitute a total from N documents. If a
trainee is stuck on *how* to test "spreading it out," point them at the
flag; let them discover on their own that N=1 is the hot case, and that
summing shards to read a total is a problem they still have to solve.

---

## 2. Unbounded Array Growth — `lab_devices`

**Setup:** four documents with a `readings` array pre-loaded at sizes 0, 200,
2,000, and 9,000 entries, simulating a device that's been appending sensor
readings into one document for a longer or shorter time instead of bucketing
them.

**Symptom (`02_lab_devices.py`):** both document size and `$push`
(the operator that appends a value to an array field) append latency scale
with existing array size (measured: 0 readings → 0.3 KB
/ 16ms append; 9,000 readings → 658 KB / ~105-120ms append). The script also
builds a scratch document with 120,000 readings (~16.6 MB) and shows it
**fails outright** — `DocumentTooLarge: BSON document too large ... supports
BSON document sizes up to 16793598 bytes`. That's the real ceiling this
pattern eventually hits, not a script bug.

**Fix — Google's general Firestore guidance names three patterns here
(separate documents/subcollections, bucketing, TTL expiry). One caveat
worth knowing before relaying this: "subcollections" is a **native
Firestore-mode** concept (a document nesting its own collections) and
doesn't exist in the MongoDB-compatible API this lab runs on at all — it's
absent from the [MongoDB 7.0 compatibility
list](https://docs.cloud.google.com/firestore/mongodb-compatibility/docs/supported-features-70)
entirely. The equivalent here is option 1 below: an ordinary flat
collection, not a nested one.**

1. **Separate documents, one per reading (recommended default).** Move
   readings out of the device document entirely, into their own collection,
   one document per reading, keyed by `(device_id, timestamp)`:

   ```javascript
   // lab_device_readings — one document per reading, not an embedded array
   db.lab_device_readings.insertOne({
     device_id: "device-00200",
     ts: new Date(),
     value: 21.5,
     unit: "celsius",
     quality: "good",
   });

   // range scan for "readings for this device in a window" replaces
   // reading the embedded array out of the device doc
   db.lab_device_readings
     .find({ device_id: "device-00200", ts: { $gte: start, $lte: end } })
     .sort({ ts: 1 });
   ```

   An index on `{device_id: 1, ts: 1}` (ESR: equality then range/sort) makes
   the range scan cheap. This has no size ceiling, no growing per-write
   rewrite cost (each insert is O(1), not O(existing array length)), and
   scales independently of how long the device has been reporting. Trade-off:
   an extra round trip if you need both the device's metadata *and* its
   recent readings, since they're no longer in one document.

2. **Bucket the array.** One document per device *per day* or *per N
   readings* (`lab_devices_readings_2026-07-14`) keeps embedding but bounds
   each document's growth. Cheaper if the read pattern wants "give me
   today's readings as one blob" and the per-bucket cap is generous relative
   to actual write volume.

3. **TTL expiry, if history doesn't need to live forever.** Complementary to
   either option above, not a replacement — it bounds *how long* data
   accumulates, not how it's shaped. On the separate-collection design, add
   a TTL index at creation time:

   ```javascript
   db.lab_device_readings.createIndex({ ts: 1 }, { expireAfterSeconds: 2592000 }); // 30 days
   ```

   Caveat worth flagging: `expireAfterSeconds` has to be set when the index
   is *created*. The compatibility list marks `collMod: expireAfterSeconds`
   (the standard MongoDB way to change an existing TTL value in place) as
   unsupported here — to change the retention window later, drop and
   recreate the index with the new value, don't reach for `collMod`.

Either way, embed only a small, bounded summary (e.g. `last_reading`) in the
parent device document for the common "give me the latest value" read —
don't make that read pay for a join/second query just to avoid the
unbounded array.

**Trap to watch for:** trainees sometimes propose "just cap the array at N
with `$push` + `$slice`" (`$slice` trims a `$push`ed array down to its last N
entries in the same update) — that's a legitimate bounded-embedding fix for small
N (e.g. "last 20 readings"), but it silently *discards* older data, which is
fine for a rolling cache and wrong for anything that needs history. Ask what
the access pattern actually requires before accepting that fix.

**Nudge, don't tell:** `--test-sizes 50,500,5000` builds a scratch document
at each given array length and reports its size/append latency, so a
trainee weighing a bucket-size cutoff can measure candidate sizes directly
instead of only the four pre-seeded points. It doesn't suggest what
threshold to pick or that bucketing is the fix — that's still theirs to
propose.

---

## 3. Sequential Index Fields — `lab_events`

**Setup:** nothing pre-seeded; `03_lab_events.py` writes into the empty
collection live, three ways by default: a strictly increasing zero-padded
counter as `_id` (`evt-000000000042`, SEQUENTIAL), a random UUID4 `_id`
(RANDOM), and a sequential counter salted with a random prefix in `[0, N)`
(SALTED, `--shard-prefixes N`, default N=3) -- unlike every other lab's
"optional" flag, SALTED is shown by default here rather than hidden behind
one, a deliberate choice to surface the fix directly instead of leaving it
for a trainee to discover (confirmed with the requester).

**Symptom:** historically measured throughput was ~658 ops/sec (sequential)
vs. ~1,087 ops/sec (random) with sequential showing a notably fatter tail
(p95 115ms / max 450ms vs. p95 30ms / max 116ms random) at only 300 ops per
scenario and 20 concurrent writers.

**Important caveat, confirmed via extensive live testing:** this backend (or
something about the Cloud Shell/GCE environment running the script) pays an
unpredictable ~5-10s cost on a single op the first time real concurrent load
hits a given region of the keyspace -- reproducible, but **not
deterministically avoidable**: the same warm-up code has produced a clean
run and a run where SEQUENTIAL alone still ate the cost, back-to-back, with
nothing else different. `03_lab_events.py`'s `warm_up()` targets this
best-effort (one random-keyed burst, one `next_sequential_id()` burst, since
a random-keyed warm-up alone does *not* clear it for SEQUENTIAL -- uuid4()
keys and `evt-<counter>` keys occupy non-overlapping regions of the
keyspace) but can't guarantee it every run. **If a trainee reports one
scenario with a wildly elevated max/wall-clock relative to its own p50,
that's very likely this artifact, not a real result** -- have them rerun
rather than draw a conclusion from it. p50 is the metric least affected by
it (a single outlier among hundreds of ops doesn't move the median).
Separately, at the default 300-op scale, live testing repeatedly found *no*
reliable p50/p95 gap between SEQUENTIAL/RANDOM/SALTED at all once the
above artifact is accounted for -- `--ops-per-writer N` sustains load over a
longer window (e.g. `--ops-per-writer 500` for 10k ops/scenario) if you want
to test whether a genuine gap emerges at real scale rather than relying on
the historical numbers above.

**Fix:** don't use a raw auto-increment or raw timestamp as the primary
write key for high-throughput writes. Either randomize (UUID, hash prefix)
or, if ordered scans matter, salt/shard the key with a bounded prefix (e.g.
`shard_03#2026-07-14T...`) so writes fan out across N prefixes while still
supporting a per-shard range scan -- exactly what the default SALTED
scenario demonstrates. Note the sibling Bigtable lab
(`~/workload_benchmark.py`, see root `CLAUDE.md`) uses exactly this
trade-off in reverse — a *reversed* timestamp to get descending scan order —
which is a good talking point: the fix depends on whether you need ordering,
even distribution, or both.

**Nudge, don't tell:** `--shard-prefixes N` still lets a trainee test a
different prefix count than the default 3 and see how it lands relative to
the other rows -- that part of the exploration is still theirs to drive,
even though the strategy itself is no longer hidden.

---

## 4. Inefficient Compound Fields — `lab_bookings`

**Setup:** 12,000 documents, no indexes beyond `_id`. Query:

```python
db.lab_bookings.find(
    {"status": "confirmed", "price": {"$gte": 200, "$lte": 800}}
).sort("created_at", -1).limit(20)
```

**Symptom (`04_inefficient_compound_fields.py`)**: the script itself only
prints one `explain("executionStats")` run, against whatever indexing
currently exists on `lab_bookings` — right after `setup_lab.py`, that's a
`COLLSCAN`. It deliberately does *not* build or compare any index for the
trainee; they're expected to add their own index(es) and re-run the script
to check. The table below is the facilitator-side reference, produced by
manually creating each indexing variant with `coll.create_index(...)`
between runs:

| Indexing | Docs examined | Read units | Time (ms) |
|---|---:|---:|---:|
| No index (COLLSCAN) | 12,000 | 566 | 44 |
| Naive: single-field index on `status` | 6,533 | 6,666 | 94 |
| Wrong compound order: `{price, status, created_at}`-style (range before equality/sort) | 6,123 | 6,271 | 363 |
| **Correct: `{status: 1, created_at: -1, price: 1}`** | **110** | **113** | **51** |
| Correct, range field dropped: `{status: 1, created_at: -1}` | 110 | 113 | 14 |

If a trainee gets stuck, the nudge is "create an index and re-run the
script" — not which index; let them land on the naive one first and see it
doesn't help.

**Common misreading to correct:** it's tempting to look at "20 results
returned" next to "566 read units" and assume read units are billed on data
*returned*. They're not — they're billed on data *scanned*. Firestore Studio
(and `explain()`) reports `dataBytesRead`; read units are that value at 4 KiB
granularity, rounded up: the COLLSCAN row's 2,316,007 bytes / 4096 = 565.43 →
**566**, matching exactly, and that byte count comes from reading all 12,000
scanned documents, not the 20 that survive the filter/sort/limit. This is
the whole mechanism the fix exploits: an index that lets the engine seek
straight to matching rows shrinks `dataBytesRead` (and therefore read
units) by shrinking what's *scanned* — what's *returned* never changes,
it's 20 either way.

The naive "just index the field I filter on" fix is a trap: it actually
**increases** read cost relative to a full collection scan (566 → 6,666 read
units) because it trades a cheap sequential scan for an index seek + fetch
over thousands of keys that still all get filtered and sorted in memory.

**Fix:** build the compound index following the **ESR rule** — **E**quality
fields first, then **S**ort fields, then **R**ange fields — and match the
sort field's direction to the query's requested direction (`created_at: -1`
here, not `1`). `{status: 1, created_at: -1, price: 1}` collapses the scan
to 110 docs / 113 read units. Interestingly the range field (`price`) can
even be dropped from the index entirely with no cost regression here, since
Firestore's planner filters the already-narrow equality+sort result set in
memory — but keep it in the index if the range predicate is very selective
and you want it eliminated before the fetch stage.

**Trap to watch for:** trainees who create an index but don't check the
*direction* (`1` vs `-1`) against the query's `.sort()` direction will often
land on the "wrong order" row above and conclude indexing doesn't help —
walk them back to checking `explain()`'s stage tree (`COLLSCAN` /
`IDXSCAN` / `SORT` / `FILTER`) rather than just re-running the query and
eyeballing wall-clock time.

Trainees will do this via `coll.create_index(...)` (MQL, the tool actually
in front of them). For reference, the equivalent via `gcloud` — useful if
you're pre-building the answer or scripting a reset — is:

```bash
gcloud firestore indexes composite create \
    --database=firestore-lab \
    --collection-group=lab_bookings \
    --api-scope=mongodb-compatible-api \
    --query-scope=collection-group \
    --field-config=field-path=status,order=ascending \
    --field-config=field-path=created_at,order=descending \
    --field-config=field-path=price,order=ascending
```

Verified live against this lab's `lab_bookings` — it drops `docs examined`
to 20 (from 12,000) and read units to 23. Two non-obvious flags: `--api-scope
=mongodb-compatible-api` is required (the default `any-api` scope is
rejected for this index shape), and it must be paired with
`--query-scope=collection-group` — omitting it and leaving the default
(`collection`) fails with `INVALID_ARGUMENT: Indexes with the
MONGODB_COMPATIBLE_API API scope must use the COLLECTION_GROUP query
scope.`

---

## 5. Sustained Traffic Ramp + Hot-Spotting — `lab_traffic_scratch`

**Setup:** Firestore's 500/50/5 guidance: start under ~500 ops/sec to a
collection, then grow by no more than ~50% every ~5 minutes so the backend
can auto-scale ahead of demand. A narrow document range under heavy
read/write/delete rates ("hot-spotting") limits how well that scaling can
keep up, regardless of ramp rate. `05_lab_traffic_scratch.py` compresses
this into seconds: it hits the *same* 30-document scratch range with the
*same* final concurrency (40 workers) two ways — SPIKE (0→40 workers
instantly, never-touched range) vs. RAMP (5→10→20→40 workers in stages,
only the final stage timed).

**Explicitly caveated in the script itself:** this is a proxy for a rule
normally observed over minutes, not a literal reproduction of the
timescale. The *mechanism* is the same one this lab already measured
directly while fixing `01_lab_counters.py`'s own benchmark
earlier in development: a key range's first exposure to real concurrency
pays a one-time backend routing/warm-up cost that an already-exercised
range doesn't (see `01`'s `run_spread()` and its comment — a single
sequential touch-per-doc wasn't enough to clear that cost; a full
concurrent warm-up burst was, dropping p95 from ~900ms to ~60ms in that
script's diagnostic).

**Symptom (measured on `labdb1`):** SPIKE: wall 1.526s, p95 1268ms, max
1510ms, est. 31.5 ops/sec. RAMP final stage (identical shape): wall 0.662s,
p95 464ms, max 644ms, est. 86.2 ops/sec — roughly 2.7x the throughput and a
third of the tail latency, from ramping the *same* total concurrency up in
stages instead of all at once. `--mode reads` reproduces the same shape for
reads (SPIKE p95 1110ms/max 1245ms/est. 36.0 ops/sec vs. RAMP p95 392ms/max
488ms/est. 102.0 ops/sec), confirming the rule's read+write scope isn't just
a write-side story here.

**Why "est. ops/sec" and not p50 or wall-clock, and a caveat shared with labs
1 and 3:** this script hits the same reproducible ~5-10s single-op latency
artifact documented under lab 1 and lab 3 below — a fixed cost the backend
pays the first time real concurrent load touches a given region of the
keyspace, not something either scenario here "does wrong." Live testing
found p50 alone actually **flips direction** run to run here (it favored
SPIKE, backwards from the lesson, in at least one clean run with no artifact
contamination at all), because this lab's real effect — a spike queuing
behind a not-yet-scaled range — shows up specifically as *tail* latency, not
a uniform shift of the whole distribution. p95 held the correct direction
(RAMP faster) across every run tried, contaminated or not, which is why
`05_lab_traffic_scratch.py` derives its printed `est. ops/sec` column as
`concurrency / p95` rather than `ops / wall-clock` (also one straggler op
away from making a fast scenario look like it crawled) or a p50-based
estimate. If a trainee's own run shows p50 disagreeing with p95 on which
scenario "won," that's expected — trust p95 here, and feel free to have them
rerun once to see it settle.

**Fix:** don't let real traffic hit a new range at full concurrency on day
one. Ramp up gradually (the literal 500/50/5 schedule in production), and
separately, avoid concentrating sustained high-rate traffic on a narrow
document range at all — the fixes from labs 1 (shard hot documents) and 3
(randomize/salt monotonic keys) are exactly how you widen a "narrow
range" in the first place. This lab is about the *rate of onset* on top of
whatever range width you already have.

**Trap to watch for:** trainees may propose "just always run at full
concurrency, since it's just a one-time cost" — true for a single lab run,
false in production, where new ranges (new shard key prefixes, new
partitions from resharding, a fresh collection after a migration) appear
continuously as an app grows. The lesson is the pattern, not this one range.

**Nudge, don't tell:** `--mode reads` is the only flag; it doesn't say why
you'd want to test reads separately — let a trainee arrive at "the rule
says read/write/delete" on their own and confirm it holds for reads too.

---

## 6. Large Reads That Return Many Documents — `lab_bookings`

**Setup:** the same 12,000-document `lab_bookings` used in lab 4, read (not
modified) two ways: one unpaginated `find({})` materializing everything
client-side, vs. paginated fetches via `.skip(n).limit(page_size)`.
`setup_lab.py` seeds this collection with an index on `_id` (`_id_1`) --
without it, `coll.index_information()` returns `{}` here (this backend, unlike
real MongoDB, does **not** auto-create a default `_id` index), and every
paginated fetch below falls back to a full `COLLSCAN` + in-memory sort
regardless of strategy, masking the exact comparison this lab is about. With
the index seeded, the two pagination strategies genuinely diverge -- see below.

**Symptom (measured on `firestore-lab`, default `--page-size 500`, 24 pages):**
UNPAGINATED: 1 round trip, wall 0.23s. PAGINATED skip/limit: 24 round trips,
wall 1.7s, first page 84ms / last page 25ms / avg 69ms. **Counterintuitive but
real: unpaginated is faster in total wall-clock time here.** The driver
already streams a `find({})` cursor via internal `getMore` continuations
reusing one server-side query plan; issuing 24 *separate* `find()` calls
(whether skip/limit or keyset) each pays this backend's fixed per-query
overhead from scratch, and 24x that overhead outweighs the unpaginated read's
single ~230ms.

**This is the point, not a bug in the demo:** the case for pagination here
isn't "it's faster" — at this lab's scale it measurably isn't. It's that
unpaginated forces the *entire* result set resident in memory at once with
no way to checkpoint or bound a single request's latency/payload size. At
real production scale (a collection too large to fit in memory, or a
result set that would blow a request timeout or response-size quota),
unpaginated isn't slower, it's not viable at all — which this 12k-doc lab
is too small to demonstrate directly. Be upfront about that distinction
rather than letting the trainee conclude "the numbers say don't paginate."

**Read units expose exactly the cost story the index makes possible:** at
`--page-size 500` (24 pages), skip/limit's read units grow from **511 on page
1 to 750 on the last page** (skip=11500) — visible even at this shallow
depth. Keyset's stay flat at **511 on every page**. Re-running at
`--page-size 50` (240 pages) makes the gap dramatic: skip/limit climbs from
**52 to 300 read units** (~6x), keyset stays flat at **52** throughout.
Latency tells a similar but smaller story (skip/limit's last page runs
slower than a flat keyset cursor at depth), but **read units are the clean,
unambiguous signal** — `skip(n)` still has to walk and discard `n` index
entries server-side even with an index behind the sort, so its cost scales
with depth; a keyset cursor (`{_id: {$gt: last_id}}`) seeks directly to its
starting point every time, so its cost doesn't.

**Fix:** paginate for *boundedness*, not raw speed — cap page size to keep
each request's latency/memory predictable — and prefer a keyset/range
cursor over `.skip(n)` once page depth matters, since skip's cost (both
read-unit and, to a lesser extent, latency) genuinely grows with depth here,
given the `_id` index this collection is seeded with. Without that index,
this distinction disappears entirely (both strategies degrade to the same
full-collection-scan cost on every page) — same root cause as lab 4, an
index that supports the query's sort/filter, showing up again at read-depth
instead of at query-shape.

**Trap to watch for:** a trainee who runs the default `--page-size 500`
comparison and only looks at wall-clock time will conclude pagination is
strictly worse — correct for this dataset's *total wall time*, misleading if
generalized. Push on *why* you'd paginate a collection that already fits
comfortably in memory. A second trap: a trainee who checks latency but not
read units may underrate the skip/limit-vs-keyset gap, since latency's
depth-growth is subtler than the read-units column at `--page-size 500`.

**Nudge, don't tell:** `--pagination keyset` adds the third row; the
script doesn't say skip/limit degrades with depth or that keyset is "the
fix" — let the trainee compare first-vs-last page numbers themselves,
across both latency and read units, before concluding anything.

**Confirmed via `firestore-mcp` (admin API, not the disabled native Documents
API):** `list_indexes` on `projects/base92124/databases/firestore-lab/collectionGroups/lab_bookings`
returns exactly one index — `_id_1` (ascending, `apiScope:
MONGODB_COMPATIBLE_API`). Both scenarios read through that same single index,
so the read-units gap isn't a missing-index story; it's how each query walks
it. `skip(n)` still has the engine step through and discard `n` index entries
before it can return a page, so backend work — and therefore read units —
scales with `n` (511 → 750 across 24 pages at `--page-size 500`). The keyset
range condition (`{_id: {$gt: last_id}}`) seeks the index straight to
`last_id`, so its backend work per page is constant (511 flat) regardless of
depth. Note `list_documents` against this database fails with `Access to this
database via the Firestore in Native mode API is disabled` — this database
was created with `mongodbCompatibleDataAccessMode: DATA_ACCESS_MODE_ENABLED`
/ `firestoreDataAccessMode: DATA_ACCESS_MODE_DISABLED`, so only the
MongoDB-compatible driver can read data; `firestore-mcp` is still useful here
for admin-plane metadata (indexes, database config) even though it can't read
documents directly.

**Official guidance:** Google's own latency-troubleshooting reference lists
"Large reads that return many documents" as a latency cause with the
resolution "Use pagination to split large reads" —
https://docs.cloud.google.com/firestore/mongodb-compatibility/docs/resolve-latency#latency.
It doesn't distinguish skip/limit from keyset cursors or discuss read-unit
cost directly, so treat the measurements above as this lab's fill-in for the
"why" the doc doesn't spell out.

---

## 7. Large Writes, Batched Writes, and Bulk vs. Batch — `lab_bulkwrite_scratch`

**Setup:** `07_batch_vs_bulk_writes.py` defines and measures the
distinction the general guidance draws: a **batch** (`insert_many` /
`bulk_write`, one atomic multi-document server op — all-or-nothing, cost
scales with how much is crammed into one atomic unit) vs. **bulk** (many
independent single-document writes issued concurrently via a thread pool —
no cross-document atomicity, but parallelized instead of queued). It writes
1000 unique new documents five ways: serial, batched at sizes 10/100/500,
and bulk (20 parallel workers).

**Symptom / important surprise (measured on `labdb1`):**

| Scenario | Wall (s) | ops/sec |
|---|---:|---:|
| SERIAL | 31.014 | 32.2 |
| BATCHED (10) | 3.541 | 282.4 |
| BATCHED (100) | 1.000 | 999.6 |
| **BATCHED (500)** | **0.923** | **1084.0** |
| BULK (20 workers) | 2.240 | 446.4 |

SERIAL is (as expected) far worse than everything else. But the ranking
above that **inverts** the general guidance's "10 beats 500" and "bulk
beats batched": here, `BATCHED(500)` won outright, and `BULK` landed
*behind* both `BATCHED(100)` and `BATCHED(500)`.

**Why, and what this means for how to use this lab:** the general guidance
("smaller batches," "parallel over batched") is about avoiding *contention*
— writes competing for overlapping keys/ranges. This script's workload has
**zero contention by design**: every one of the 1000 documents is new and
unique, same as a real bulk CSV/data import. With nothing to contend over,
what dominates instead is this backend's fixed per-request overhead (the
same effect lab 6 measured for reads: many separate round trips cost more
in aggregate than fewer, larger ones) — and a big atomic batch amortizes
that overhead across more documents than 20-way parallel individual writes
can beat. **Don't present the "10 beats 500, bulk beats batch" ordering as
a fact this script will confirm — it's the opposite of what it measures for
a non-contended bulk load, and that's the actual lesson**: the general
heuristic's mechanism is contention-avoidance, so it applies when writes
conflict, not automatically to every bulk-write scenario. A trainee who
wants to see the classic ordering should look back at lab 1 or 5, where
writes *do* overlap on the same narrow key range.

**Fix / framing for trainees:** for a genuinely non-contended bulk load
(unique keys, no conflicts), batch as large as your atomicity requirements
and payload/size limits allow — don't reflexively fragment into many small
batches or many parallel individual writes if nothing is contending. Reach
for bulk-parallel individual writes specifically when either (a) you need
failure isolation (one bad doc shouldn't abort the other 999), or (b) the
writes *do* contend and a large atomic batch would serialize/abort under
that contention — situations orthogonal to raw round-trip-count
minimization.

**Trap to watch for:** a trainee may try to "fix" this result by increasing
`--batch-sizes` further and generalize "bigger is always better" — worth
having them push batch size well past 500 (the doc's own reference point)
to see whether/where the trend reverses (very large single-batch payloads
have their own size and latency ceilings), rather than accepting either
extreme as universal.

**Nudge, don't tell:** `--batch-sizes 5,50,1000` (or any other set) is the
only flag — it doesn't hint at what the "right" ceiling is, or that the
ordering will differ from the general guidance. Let the trainee discover
the inversion themselves and reason about why before you explain
contention as the missing variable.
