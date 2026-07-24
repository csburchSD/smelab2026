# Firestore Anti-Patterns Lab — Facilitator Guide

Setup, architecture, and the answer key, all in one place. Don't hand this
to trainees — give them `LAB_GUIDE.md` instead; it has no fixes given away.
Symptom numbers below are from real measurements against a live database
(the specific database is noted per lab where it matters); re-running will
vary but the relative shape should hold.

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
  before it tries to create anything. This matters because `gcloud`'s own
  "enable it now?" prompt defaults to declining even under `--quiet`, so a
  brand-new project would otherwise fail with a `SERVICE_DISABLED` error.)
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

If propagation takes longer than that ~2-minute budget, the script gives up
waiting, skips seeding, and tells you to run
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
`coll.drop()`. `roles/datastore.user` doesn't cover collection/index
administration, so `drop()` fails with `PermissionDenied` no matter how long
you wait for propagation. `roles/datastore.owner` is what this lab actually
needs.

**IAM propagation delay:** a SCRAM user can authenticate successfully
immediately, but every read/write fails with `PermissionDenied` until the
IAM binding above finishes propagating — typically a couple of minutes.
Don't assume the binding command failed if the very next write attempt
still 403s; retry after a short wait.

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

**Symptom (`01_lab_counters.py`):** 20 concurrent writers doing 100 total
`$inc` ops against one shared document run materially slower than the same
100 ops spread across 20 documents (measured: ~95ms hot vs. ~14ms cold).
Firestore serializes writes to a single document; concurrent writers queue
behind each other.

**Fix:** shard the counter. Maintain N separate shard documents (e.g.
`global_stats_shard_0..9`), write to a randomly chosen shard per increment,
and sum shards to read the total. (These are separate top-level documents in
the collection, not sub-documents/embedded documents.) This is the standard
"distributed counter" pattern (same idea Firestore's own docs recommend for
native mode). For non-counter hot documents (e.g. a single "settings" doc
every request reads *and* writes), the fix is usually to split read-heavy
and write-heavy fields into separate documents instead.

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
transient failure) — the app must.

**Nudge, don't tell:** `--docs N` spreads the same total workload across N
documents (N=1 reproduces HOT, N=20 reproduces COLD) without the script
saying why that helps or how to reconstitute a total from N documents. If a
trainee is stuck on *how* to test "spreading it out," point them at the
flag; let them discover on their own that N=1 is the hot case, and that
summing shards to read a total is a problem they still have to solve.

**Identification, if you have Key Visualizer access:** a hot single document
shows up as one thin, bright horizontal line in Key Visualizer's writes
heatmap — one row getting hit far harder than its neighbors. Elevated
`ABORTED`/`DEADLINE_EXCEEDED` rates against one specific document path in
application logs are the same signal without needing Key Visualizer access.

**Related, not reproduced by this script:** client-side debouncing/batching
(buffer non-critical updates and flush periodically instead of writing on
every event) is a legitimate complementary fix when writes don't need
per-event durability. Separately, **large multi-document transactions** are
a related contention risk this lab doesn't exercise directly (every op here
is a single-document `update_one`) — the same principle extends to explicit
transactions: keep them narrowly scoped to what actually needs atomicity,
move reads/computation that don't need it outside the transaction, and
prefer a plain batched write (`bulk_write`/`insert_many`, see lab 7) over a
transaction whenever nothing in the batch needs to be read back first.

---

## 2. Unbounded Array Growth — `lab_devices`

**Setup:** four documents with a `readings` array pre-loaded at sizes 0, 200,
2,000, and 9,000 entries, simulating a device that's been appending sensor
readings into one document for a longer or shorter time instead of bucketing
them.

**Symptom (`02_lab_devices.py`):** both document size and `$push`
(the operator that appends a value to an array field) append latency scale
with existing array size. The script also builds a scratch document with
120,000 readings (~16.6 MB) and shows it
**fails outright** — `DocumentTooLarge: BSON document too large ... supports
BSON document sizes up to 16793598 bytes`. That's the real ceiling this
pattern eventually hits, not a script bug. (Worth knowing the ceiling
differs by edition: Standard/Native-mode Firestore documents cap at 1 MiB;
this lab's Enterprise MongoDB-compatible API caps at ~16 MiB, matching
MongoDB's own BSON limit, as shown by the error above. Either way, the
fixes below apply regardless of which ceiling you're up against.)

**Fix:**

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

4. **Offload genuinely large payloads to object storage.** If what's
   growing isn't "a long list of small readings" but actual large blobs
   (images, files, long text), store the blob in Cloud Storage and keep only
   a reference URL in the Firestore document — this sidesteps the
   document-size ceiling entirely rather than working around it.

5. **Cap the array with `$push` + `$slice`, for a bounded rolling window.**
   (`$slice` trims a `$push`ed array down to its last N entries in the same
   update.) Valid when the access pattern only ever needs "the last N" (e.g.
   last 20 readings) — but it silently *discards* older data, so it's the
   wrong choice for anything that needs full history.

Either way, embed only a small, bounded summary (e.g. `last_reading`) in the
parent device document for the common "give me the latest value" read —
don't make that read pay for a join/second query just to avoid the
unbounded array.

---

## 3. Sequential Index Fields — `lab_events`

**Setup:** nothing pre-seeded; `03_lab_events.py` writes into the empty
collection live, three ways by default: a strictly increasing zero-padded
counter as `_id` (`evt-000000000042`, SEQUENTIAL), a random UUID4 `_id`
(RANDOM), and a sequential counter salted with a random prefix in `[0, N)`
(SALTED, `--shard-prefixes N`, default N=3) -- unlike every other lab's
"optional" flag, SALTED is shown by default here rather than hidden behind
one, a deliberate choice to surface the fix directly instead of leaving it
for a trainee to discover.

**Lead with the mechanism, not the table:** Firestore stores keys
**lexicographically**. A monotonically increasing `_id` means every new
write lands at the same, constantly-advancing edge of that lexicographic
order, concentrating write volume on a narrow part of the backend's key
range instead of spreading it across ranges the backend already knows about
— the reason behind Google's own "avoid sequential keys" guidance. A
randomized or salted key has no single "latest" edge to concentrate on.

**Identification, if you have Key Visualizer access:** look for a bright,
concentrated band advancing along one edge of the writes heatmap, as
opposed to lab 1's fixed single-row line — same tool, applied to a moving
hotspot instead of a static one.

**A MongoDB-specific wrinkle worth flagging:** if a trainee suggests "just
don't set `_id`, let the driver generate one" as the fix, point out that
this API's driver-default (a BSON `ObjectId`) is **not** the same as
Native-mode Firestore's fully-random auto-ID — an `ObjectId`'s leading 4
bytes are a Unix timestamp, so documents inserted in the same second still
share that prefix, giving driver-default IDs a mild version of the same
moving-edge problem. That's exactly why this script's RANDOM scenario
explicitly uses `uuid4()` rather than omitting `_id` and relying on the
driver default — worth surfacing as the more precise fix if a trainee
reaches for "just let the driver handle it."

**Related, not reproduced by this script:** the same lexicographic-hotspot
mechanism also hits **indexed field values**, not just `_id` — e.g. a
`createdAt` timestamp field that's indexed and constantly written with
"now," or a `status` field where most documents share one of a few values.
The fix family is the same (avoid ever-increasing or narrowly-clustered
indexed values under high write rates); Native/Datastore mode also offers a
single-field index exemption for fields that never need to be queried or
sorted on.

**Why there's a "Drift" column:** rather than only comparing scenarios'
overall latency to each other (vulnerable to the
startup artifact landing differently in each scenario), each row also
reports its own second-half p50 vs. first-half p50, within that one
scenario's run -- a monotonic key's hot edge getting harder to serve as it
accumulates writes should show up as SEQUENTIAL's drift trending positive
while RANDOM/SALTED stay near 0%, even on a run where the cross-scenario
comparison is a wash. Both halves' medians are independently immune to the
one-off startup outlier landing in either half, so this reads cleanly
regardless of the artifact above. If a trainee sees flat drift across all
three scenarios even at a pushed-up `--ops-per-writer`, that's a legitimate
result at this lab's achievable scale -- fall back to the mechanism
explanation above rather than treating it as a broken measurement.

**Fix:** don't use a raw auto-increment or raw timestamp as the primary
write key for high-throughput writes. Either randomize (UUID, hash prefix)
or, if ordered scans matter, salt/shard the key with a bounded prefix (e.g.
`shard_03#2026-07-14T...`) so writes fan out across N prefixes while still
supporting a per-shard range scan -- exactly what the default SALTED
scenario demonstrates.

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

For reference, the equivalent via `gcloud` — useful if you're pre-building
the answer or scripting a reset — is:

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

---

## 5. Sustained Traffic Ramp + Hot-Spotting — `lab_traffic_scratch`

**Setup:** Firestore's 500/50/5 guidance: start under ~500 ops/sec to a
collection, then grow by no more than ~50% every ~5 minutes so the backend
can auto-scale ahead of demand. (Enterprise edition specifically handles
higher bursts than these numbers suggest, per Google's own docs — treat
500/50/5 as the safe general guideline worth teaching, not a hard ceiling
this particular edition enforces.) A narrow document range under heavy
read/write/delete rates ("hot-spotting") limits how well that scaling can
keep up, regardless of ramp rate. `05_lab_traffic_scratch.py` compresses
this into seconds: it hits the *same* 30-document scratch range with the
*same* final concurrency (40 workers) two ways — SPIKE (0→40 workers
instantly, never-touched range) vs. RAMP (5→10→20→40 workers in stages,
only the final stage timed).

**Explicitly caveated in the script itself:** this is a proxy for a rule
normally observed over minutes, not a literal reproduction of the
timescale. The *mechanism* is the same one described under lab 1's caveat
above: a key range's first exposure to real concurrency pays a one-time
backend routing/warm-up cost that an already-exercised range doesn't.

**Fix:** don't let real traffic hit a new range at full concurrency on day
one. Ramp up gradually (the literal 500/50/5 schedule in production), and
separately, avoid concentrating sustained high-rate traffic on a narrow
document range at all — the fixes from labs 1 (shard hot documents) and 3
(randomize/salt monotonic keys) are exactly how you widen a "narrow
range" in the first place. This lab is about the *rate of onset* on top of
whatever range width you already have.

**Identification, if you have Key Visualizer access:** a narrow range under
heavy traffic shows as a bright, concentrated block in the heatmap (the
whole active range lighting up, not a single thin line like lab 1's hot
document) — correlate with elevated `ABORTED`/`DEADLINE_EXCEEDED` rates in
application logs for a Key-Visualizer-free version of the same signal. The
internal Anti-pattern Analyser Colab, if you have access, automates spotting
several of labs 1/3/5's hotspot shapes directly from Cloud Monitoring data.

**One cause this lab doesn't reproduce directly:** a high **delete** rate
against a narrow range is the same mechanism with the traffic direction
reversed. If a trainee's own system needs bulk cleanup, prefer a TTL policy
(automatic, backend-paced) or a soft-delete flag processed gradually in the
background over a tight loop of deletes against adjacent keys.

---

## 6. Large Reads That Return Many Documents — `lab_bookings`

**Setup:** the same 12,000-document `lab_bookings` used in lab 4, read (not
modified) two ways: one unpaginated `find({})` materializing everything
client-side, vs. paginated fetches via `.skip(n).limit(page_size)`.

**This lab is literally two named anti-patterns at once:** UNPAGINATED is
Google's own "queries without limits" anti-pattern (an unbounded `find({})`
with no `.limit()`), and PAGINATED skip/limit is "offset in pagination"
(`.skip(n)` is this API's equivalent of Native mode's `.offset()`) — both
documented causes of ballooning read cost as a collection grows. Worth
naming both explicitly if a trainee wants the official terminology.

**This is the point, not a bug in the demo:** the case for pagination here
isn't "it's faster" — at this lab's scale it measurably isn't. It's that
unpaginated forces the *entire* result set resident in memory at once with
no way to checkpoint or bound a single request's latency/payload size. At
real production scale (a collection too large to fit in memory, or a
result set that would blow a request timeout or response-size quota),
unpaginated isn't slower, it's not viable at all — which this 12k-doc lab
is too small to demonstrate directly.

**Read units expose exactly the cost story the index makes possible:** at
`--page-size 500` (24 pages), skip/limit's read units grow from **511 on page
1 to 750 on the last page** (skip=11500) — visible even at this shallow
depth. Keyset's stay flat at **511 on every page**. `skip(n)` still has to
walk and discard `n` index entries server-side even with an index behind
the sort, so its cost scales with depth; a keyset cursor (`{_id: {$gt:
last_id}}`) seeks directly to its starting point every time, so its cost
doesn't.

**Fix:** paginate for *boundedness*, not raw speed — cap page size to keep
each request's latency/memory predictable — and prefer a keyset/range
cursor over `.skip(n)` once page depth matters, since skip's cost (both
read-unit and, to a lesser extent, latency) genuinely grows with depth here,
given the `_id` index this collection is seeded with. Without that index,
this distinction disappears entirely (both strategies degrade to the same
full-collection-scan cost on every page).

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
conflict, not automatically to every bulk-write scenario.

**Fix / framing for trainees:** for a genuinely non-contended bulk load
(unique keys, no conflicts), batch as large as your atomicity requirements
and payload/size limits allow — don't reflexively fragment into many small
batches or many parallel individual writes if nothing is contending. Reach
for bulk-parallel individual writes specifically when either (a) you need
failure isolation (one bad doc shouldn't abort the other 999), or (b) the
writes *do* contend and a large atomic batch would serialize/abort under
that contention.

**Worth knowing, not isolated as a variable in this script's own numbers:**
every document here is inserted without setting `_id`, so the driver
assigns default BSON `ObjectId`s, which share a timestamp prefix for all
documents inserted within the same second (see lab 3's note on this).
Since all 1000 docs complete in a few seconds, this workload's own `_id`s
are more lexicographically clustered than a random-UUID workload would be
— and Google's own guidance says to keep documents in a high-rate bulk
batch from being lexicographically close. Whether that measurably affects
BULK's standing here specifically hasn't been tested live; flag it as an
honest open question if a sharp trainee asks "wait, aren't these `_id`s
kind of sequential too?" rather than asserting it doesn't matter.

---

## Related anti-patterns this lab suite doesn't reproduce

A few more anti-patterns from Google's internal reference material are worth
knowing for Q&A, even though no script here reproduces them directly:

- **Skipping over many recently deleted documents (tombstones).** A query
  that orders by an ever-growing field (e.g. `createdAt`) and only cares
  about "live" rows — a task queue polling for `status == 'PENDING'` and
  deleting on completion is the classic case — ends up scanning past index
  tombstones for every already-deleted row before reaching live data, visible
  as query latency degrading over time even though the live document count
  stays flat. Fix: filter aggressively on an active-set field (e.g. index
  `(status, createdAt)` and query `status == 'PENDING'`) instead of ordering
  by the raw timestamp alone, or use TTL to age out old tombstones faster.
- **Dynamic collection or field names in place of a shared schema** (e.g. a
  collection per customer, `customer_283993`, instead of one `customers`
  collection with a `customer_id` field; or a boolean field per tag,
  `tag_cloud: true`, instead of a `tags: ["cloud"]` array). Multiplies index
  metadata and can hit index-count limits fast. Fix: use a shared
  collection/schema and differentiate with field values, not with dynamic
  names.
- **Composite index combinatorial explosion.** Every composite index
  Firestore creates also implies "shadow" indexes for other sort-direction
  combinations of the same fields — defining many overlapping composite
  indexes multiplies write cost across all of them. If a trainee has real
  production indexes to review, the useful question is whether they're all
  still queried, not just whether they were once useful.
