# Nautobot performance harness

A two-tier measurement setup for finding and fixing performance problems in
Nautobot core, plus the ground rules for the optimization loop.

## Environment

This stack is deliberately isolated so it can coexist with another local
Nautobot instance:

| | |
|---|---|
| Compose project | `nautobot-perf-3-2` |
| Nautobot web | http://localhost:8180 |
| celery_worker | 8181 |
| Postgres / Redis | not published to the host |
| `DEBUG` | `False` (no debug toolbar, no SQL logging) |
| `PLUGINS` | `[]` — core only, so every finding is attributable and every fix committable |
| Resources | pinned CPU/memory (see `development/docker-compose.perf.yml`) so runs are comparable |

Config lives in `invoke.yml` and `development/docker-compose.perf.yml`.

## Why two tiers

**Tier 1 (`tier1_queries.py`) — SQL query profiling.** Drives every endpoint
through the Django test client and records query count, duplicate-query count,
and DB time. These numbers are *deterministic*: they don't move with machine
load, so they work as a regression gate even on a small dataset where wall-clock
differences are buried in noise. This is where N+1 patterns and missing
`select_related`/`prefetch_related` show up.

**Tier 1W (`tier1w_writes.py`) — write-path query profiling.** Tier 1 only
exercises GET, so change logging, signals and validation are invisible to it.
Each operation runs inside `web_request_context` (change logging and webhook
processing behave as for a real write) and inside a transaction that is rolled
back afterwards, so runs are repeatable. Nautobot creates ObjectChange records
from a synchronous `post_save`/`m2m_changed` receiver, so rollback does not hide
change-logging cost.

The bulk triple is the informative part. All three create the same 100 rows:

| operation | change logging |
|---|---|
| `.loop` | inline, per object — what naive code does |
| `.deferred` | batched via `deferred_change_logging_for_bulk_operation()` — what bulk-edit views do |
| `.bulk_create` | none; also skips validation and signals — the floor |

The deltas isolate what change logging actually costs. Note that the deferred
path still calls `to_objectchange()` once per object at flush time
(`context_managers.py:115`), so it pays the same double serialization as the
inline path — just later, and over more objects.

**Tier 2 (`tier2_latency.py`) — wall-clock latency via cassowary.** Confirms
that a query-count win is a real latency win.

Two constraints shaped the Tier 2 driver, both verified empirically:

- cassowary reports metrics **only in aggregate**. Its JSON summary carries just
  `base_url`, and `-R` raw CSV columns are
  `DNSLookup,TCPConn,TLSHandshake,ServerProcessing,ContentTransfer,StatusCode,TotalDuration`
  — no URL column. So file-slurp mode over the whole endpoint list gives one
  blended number that identifies nothing. The driver runs cassowary once per
  endpoint instead.
- Timings are **integer milliseconds**. Fine for views in the 50–500ms range,
  useless below ~10ms. Another reason Tier 1 carries the signal at small scale.

## The workload

`workload.yml` defines the accesses we measure. It is hand-maintained on
purpose. The alternative -- `nautobot-server generate_performance_test_endpoints`
-- dumps every GET URL the resolver knows about, which is both unmaintained and
unusable as a fixture: it bakes concrete PKs into a file, so it breaks on every
data reload.

Two properties make `workload.yml` repeatable:

- Endpoints are named by **Django view name** and resolved with `reverse()`.
  A renamed view fails loudly at resolve time instead of silently rotting.
- Objects are chosen by a deterministic **strategy** evaluated at run time --
  never a baked-in PK -- so the workload survives a reseed or a different
  dataset. `first`/`last` order by pk; `max_related:<name>` picks the object
  with the most of something (the worst case, and the one most likely to expose
  per-row work), with a pk tie-break for stability.

Coverage is chosen rather than exhaustive: the wide list views with many related
columns, the heavy detail pages, IPAM's hierarchy computation, the changelog,
and the API mirrors of the key reads -- including `depth=1`, which drives nested
serialization, and deep offsets.

Unresolvable scenarios are reported and recorded in the run's `unresolved`
field, so shrinking coverage is visible rather than silent.

## Workflow

```bash
invoke build && invoke start
invoke migrate
invoke createsuperuser

# 1. Generate and load the dataset
databot generate --archetype enterprise-campus --scale small --seed 42 -o perf/dataset.yaml
NAUTOBOT_URL=http://localhost:8180 NAUTOBOT_TOKEN=<token> databot apply perf/dataset.yaml

# 2. Snapshot, so every experiment starts from identical state
databot dump

# 3. Baseline. Tier 1 resolves the workload and dumps the URL list for Tier 2.
docker compose exec nautobot python /source/perf/tier1_queries.py \
    --out /source/perf/baselines/tier1-baseline.json \
    --dump-urls /source/perf/results/urls.json
python3 perf/tier2_latency.py --urls perf/results/urls.json \
    --urls-from-tier1 perf/baselines/tier1-baseline.json --top 25 \
    --out perf/baselines/tier2-baseline.json
```

## The optimization loop

One experiment per commit. For each:

1. Restore the snapshot so state is identical to the baseline.
2. Make the change.
3. Re-run Tier 1; `compare.py` against the baseline.
4. Keep only if `compare.py` exits 0 **and** shows a real improvement. Otherwise revert.
5. Commit with the hypothesis, the method, and the before/after numbers.

```bash
python3 perf/compare.py --baseline perf/baselines/tier1-baseline.json \
                        --current  perf/results/tier1-current.json
```

`compare.py` exit codes: `0` pass, `1` query regression, `2` endpoint broken.

**The hard invariant is availability.** An endpoint that returned 200/302 at
baseline must not start returning 5xx (or raising) after a change. A drop to 4xx
is treated the same way -- a detail page that starts 404ing is broken by any
reading.

Response **content and row ordering are allowed to change** and are reported for
information only. Tier 1 records a content hash and, for JSON list responses, a
separate hash of result identities, so the report can distinguish "rows
reordered" from "row content differs" -- useful context when judging an
experiment, but not a gate.

Endpoints already failing at baseline are listed separately and never gate: the
invariant is "nothing degrades relative to baseline", not "everything is 200".

## What this exercise is for

**This is exploration, not a shipping queue.** The goal is to establish what is
possible in Nautobot's read and write paths, measured well enough that someone
else can decide what to do about it. Code here may never reach production, and
that is an acceptable outcome for any individual finding.

That has one consequence worth stating plainly, because it inverts the usual
priority: **the report and the commit ledger are the deliverable, not the merged
tree.** A finding whose numbers are right and whose write-up is wrong has failed.
A finding that is correct, priced, and never shipped has succeeded.

So the standard is not "would we ship this" but "is this priced precisely enough
that someone who owns the product can judge it." That means blast radius
enumerated rather than gestured at -- the way the `object_data` experiment names
two API contracts, one UI panel and 13 specific tests -- and it means a caveat
written as a release note would have to write it.

## The gate comes before the taxonomy

A change must **be faster** and must not break availability. Nothing else
matters until those hold:

- `compare.py` exits 0. An endpoint that returned 200/302 at baseline must not
  start erroring or 404ing. Content and row ordering may change and are reported
  for information only.
- The improvement is real on an instrument that can see it, with the arms proven
  to differ by a deterministic counter.

A change that fails the gate is not a low tier -- it is not a finding at all.
The rejected FK pre-warming experiment removed 666 queries and ran 62% slower;
that is a failed gate, not a Tier C change. Keep the two ideas apart.

## Risk taxonomy: two axes and two flags

Everything that passes the gate gets labelled. The label is a **price tag, not a
filter** -- no cell is forbidden, and the expensive cells are often the most
useful things on the branch, because they tell a reader what a tempting option
actually costs.

**Behaviour axis** -- what a user or an API client can observe:

| | |
|---|---|
| **A** | No observable change. No new shared state. |
| **B1** | New state scoped to a request, a transaction, or an object instance, and discarded with it. Nothing to invalidate, because nothing survives the scope. |
| **B2** | New state that outlives its scope and therefore needs invalidating. Requires an enumerated invalidation path and a **bounded** staleness window. |
| **C** | Changes observable behaviour -- an API payload, a rendered value, a documented default. |

The B split is not theoretical. Two experiments were rejected for the same
reason A/B/C could not express: a cross-request natural-key map (`.update()` and
`.bulk_update()` compile to one SQL statement and emit no signal, so the
staleness window was unbounded) and a process-level per-user nav menu cache
(permission edits taking effect only after expiry). Both looked
"result-preserving but subtle". Both were B2, and that is why they died.

**Migration axis** -- what deploying it costs an operator:

| | |
|---|---|
| **—** | No migration. Where every accepted change on this branch currently sits. |
| **M0** | Metadata-only. `AlterField(null=True)`, an index rename. No table rewrite. |
| **M1** | A bounded single pass, with a measured per-row cost and a stated bound on production row counts. |
| **M2** | Unbounded, or bounded only by operator configuration. Priced and handed off. |

A migration is in scope if it is one mechanical Django operation, reversible,
leaves no public API payload changed once the accompanying code lands, and
**arrives with its own measured runtime**. Every other change here carries
before/after numbers; a migration must too, because its cost lands on operators
rather than on us.

Out of scope entirely -- and this is the line worth holding -- is anything that
changes what a model *means*: new relationships, denormalisation, splitting or
merging tables, redefining a natural key. Redesign is a different exercise.

**Flags**, which cut across both axes and make any cell stricter:

- **`security-visible`** -- the staleness or behaviour change touches
  authorisation rather than display. A bounded stale window on a display string
  is tolerable; the same window on `is_superuser` is not. This is why a
  per-user nav cache worth ~0.6ms was rejected.
- **`third-party-coupled`** -- depends on internals of a dependency that can
  change without failing loudly. Not a correctness risk today; a maintenance
  risk that fires on upgrade, so it belongs in upgrade notes. The caching
  `TemplateColumn` reimplements django_tables2 3.0.1's `render()` and is the
  case in hand.

**Every finding outside A × — carries a caveat, written as a release note would
write it.** "Tier C, changes API output" is a label. *"Composite keys change for
Location and every device component; a CSV exported before the change no longer
round-trips"* is something a product owner can weigh. The second one is the
deliverable.

## One correctness stop that pricing does not rehabilitate

Expensive is a price. Wrong is not. The `object_data_v2` backfill is the
worked example: v1 stores foreign keys as bare primary keys while v2 needs
nested natural keys, so reconstructing v2 for a row that references a
since-deleted object is not possible at any cost. Record why, and stop.

## Findings are structured data, and the report is generated

`perf/findings/*.yml` is the source of record for every experiment: its tier,
flags, caveat, instruments, controls, tests and status. `perf/build_report.py`
renders `perf/report.md` from those files plus the committed baselines, filling
the `<!--GEN:...-->` markers in `perf/report.template.md`. Narrative prose stays
in the template, where writing it by hand is the point.

    python3 perf/build_report.py            # write perf/report.md
    python3 perf/build_report.py --check    # exit 1 if it has drifted

This exists because the report drifted five commits behind the tree once and
carried a figure a later commit had already retracted. Under a framing where the
report *is* the deliverable, hand-maintaining it is the weakest link. Numbers
live in one place; the report reads them.

Markdown rather than HTML, deliberately. A markdown diff shows which number
moved, so drift is visible in review rather than merely detectable by `--check`
-- and it removes escaping and tag-balancing from a tool whose entire job is not
being wrong.

## Measurement notes

`pg_stat_statements` is loaded in the perf overlay. It's measurement only — it
attributes total DB time across a run, which is how the non-actionable index
candidates get evidence rather than guesses.

## Scale note

Run 1 uses `enterprise-campus small` (2,113 objects: 231 devices, 740
interfaces, 272 cables, 231 IPs). That is enough to prove the pipeline and to
surface N+1 patterns in query counts, but most list views paginate at 25–50 rows
and will look fast regardless. Re-baseline at `large` (~24k objects) before
trusting any optimization's real-world value. The report marks each finding as
query-count-only or latency-confirmed.

## Future: workload derived from customer demo recordings

The 39 scenarios in `workload.yml` were chosen by hand for *diagnostic coverage* --
deliberately including worst cases like `?depth=1` -- which is close to the opposite of a
usage-weighted sample. `compare.py` therefore weights every scenario equally, which is
almost certainly wrong relative to real usage.

A better source than either guesswork or production logs: the recorded demos NTC uses with
customers and prospects. Those encode workflows that were refined *because* they illustrate
real use cases, where production logs would be dominated by whatever one customer's
integrations happen to poll.

Sketch, not yet built:

1. `ffmpeg -vf fps=1` to extract frames; read them to produce an ordered trace of page
   identity, action, and dwell time. UUIDs do not need to be recovered -- the resolver picks
   objects by strategy, so "a device with many interfaces" is sufficient.
2. Map to Django view names and resolve through `reverse()`, as `workload.yml` already does,
   so anything that no longer exists fails loudly.
3. Replay the sequence once with `OTEL_PYTHON_DJANGO_INSTRUMENT` enabled (see
   `development/docker-compose.observability.yml`) to capture the real request fan-out per
   page. This is the step that matters: one page view can be 1 HTTP request or 30, and every
   optimization on this branch lives at request granularity.
4. Emit weights into `workload.yml` and have `compare.py` report weighted alongside unweighted
   totals, so existing numbers stay comparable.

Caveat to carry forward: demo workflows over-represent the narratively interesting and
under-represent boring bulk -- integration polling, a list view left open. Useful for "does
Nautobot feel good in the situations we sell on", not a complete picture of load.

## Open: the full test suite has never completed

`invoke tests` (whole suite) has not run to completion on this branch. The one attempt
died with:

    multiprocessing.pool.MaybeEncodingError: Error sending result:
    '<multiprocessing.pool.ExceptionWithTraceback object ...>'

That is Django's parallel runner failing to pickle a worker exception back to the parent --
a harness failure, not a test failure, and it hides whatever the underlying exception was.
**It exited with code 0**, which is the third time on this branch that a pipeline's exit
status has masked a failure; check the output, not `$?`.

To get a real answer:

    invoke tests --no-parallel          # serial, so worker exceptions surface directly

Two things to know before reading the result:

- The Tier C commit (`object_data={}`, `43db1b018`) knowingly breaks 13 tests in
  `nautobot.extras.tests.test_changelog`. It is **not** in the working tree -- it was
  reverted in `a8f7dbd60` and never restored -- so a clean run is expected. If those 13
  reappear, something restored it.
- `test_get_docs_url` fails across ~35 dcim model tests whenever `--skip-docs-build` is
  passed, on a clean tree too. Run without that flag, or discount those specifically.

Per-experiment targeted tests have all passed, including `nautobot.dcim.tests.test_api`
(1673) and `nautobot.ipam.tests.test_views` (596). The gap is a single whole-suite run.

## Open: systematic CRUD/list coverage matrix

`workload.yml` has 39 hand-picked scenarios. The actual API surface is **330 list
endpoints, 320 detail endpoints and 127 UI list views across 14 apps** -- so coverage is
roughly 5%, and the scenarios were chosen for diagnostic interest, which is a selection
bias, not a sample.

The argument for building it: `api.interface.depth1` turned out to be a 74% win, and it was
found by hand-guessing that "interfaces is the biggest table". There are ~300 endpoints
nobody has looked at.

Design, not yet built:

- **Enumerate from the URL resolver at run time**, exactly as `workload.py` resolves view
  names, so the matrix cannot rot as models come and go.
- **Reads first** -- `list`, `list?depth=1`, `detail`, `detail?depth=1` per model. No
  payloads, no mutation, safe to re-run. ~1200 measurements; Tier 1 does 38 in about a
  minute, so budget roughly half an hour.
- **Normalize to cost per object**, not per request. That is what makes it a screening
  instrument: a 5-row model becomes comparable to an 8925-row one, and anomalies surface
  regardless of table size. Rank by queries-per-row and ms-per-row.
- **Writes are the harder half, and databot is the payload factory.** create/update need
  schema-valid payloads per model, which databot already generates from the OpenAPI schema
  and OPTIONS metadata. The open problem is isolation: REST writes cross the process
  boundary, so the rolled-back transactions `tier1w_writes.py` relies on do not apply. Run
  the write matrix as a batch against a restored snapshot, then restore again.

**This is a screening pass, not a regression gate.** 1200 measurements do not belong in the
inner loop -- that stays at 38 scenarios. Run this occasionally; its output is a ranked list
of where to point the next investigation.

## Environment quick reference

| | |
|---|---|
| Nautobot UI | http://localhost:8180 (admin / admin) |
| API token | `0123456789abcdef0123456789abcdef01234567` |
| celery_worker | 8181; Postgres and Redis are not published to the host |
| Compose project | `nautobot-perf-3-3` (all commands via `perf/dc.sh`) |
| Dataset | databot `enterprise-campus / large / seed 42`, 24,091 objects |
| Restore | `perf/restore_snapshot.sh` (uses `perf/snapshot-large.sql`, gitignored) |

## Parity checklist for an apples-to-apples environment comparison

Standing up a replica of a hosted instance only yields a valid comparison if the variables
below match. Each one here either was measured to matter during this work, or is a known
way to invalidate the result.

**Ask before building anything.** These two questions may answer the whole thing without a
replica:

- **Shared or dedicated vCPU?** DigitalOcean Basic droplets are shared-vCPU and throttle
  under sustained load once burst credits are spent. That alone can produce a multiple-x
  gap, and no application change recovers it.
- **What is the steal time?** `vmstat 1 10`, watch the `st` column. Non-zero means the
  hypervisor is taking CPU. Add `nproc` and, if containerized, `cat /sys/fs/cgroup/cpu.max`.

**Compute**
- droplet class (Basic / General Purpose / CPU-Optimized) and size
- vCPU count and whether CPU is pinned; container CPU/memory limits if containerized
- steal time under load, not just at idle

**Storage** -- the usual hidden variable for a database
- local NVMe vs network-attached block storage
- IOPS ceiling and whether it is being hit (`iostat -x 1`)

**Postgres**
- managed DO database vs on-droplet; if managed, the network hop is real
- version, `shared_buffers`, `work_mem`, `effective_cache_size`, `max_connections`
- `pg_stat_statements` for comparison. Local reference from this work: **325,152 queries in
  4.6 seconds total** across a full baseline run, mean 0.014ms. If the replica or the demo
  is far off that, the database is implicated; if it matches, it is exonerated.

**Redis** -- managed vs local. Measured here: one endpoint was doing 1,938 Redis round-trips
against 977 SQL queries, so a network hop to Redis is not a rounding error.

**Application**
- gunicorn worker count, worker class, timeouts
- `DEBUG`, and the full `PLUGINS` list. This branch runs `PLUGINS = []` deliberately so
  findings are attributable to core; a demo instance with Apps installed is not comparable
  without matching them, and Apps add nav-menu items, middleware and context processors.
- Nautobot version. next.demo.nautobot.com reports API version 3.3, same as this branch, so
  version was *not* a confound in the one comparison done here.

**Dataset -- shape, not just size**
- next.demo.nautobot.com: 1,305 devices, 24,700 interfaces, 125 locations, 1,229 prefixes
- this environment: 2,902 devices, 8,925 interfaces, 110 locations, 595 prefixes
- Note the demo has *fewer* devices but ~2.8x the interfaces. Since most costs here proved
  page-bounded rather than dataset-bounded, that matters less than it looks -- but the
  hierarchy endpoints do scale with table size, so Location and Prefix counts should match.

**Edge**
- where TLS terminates, reverse proxy, geography. Measured from here, TLS handshake to the
  demo was ~70ms, so network was not the story -- server time was 1,051ms for 50 devices.

**Measurement note.** `perf/tier2_latency.py` runs against any URL with a token, so it works
against a hosted instance unchanged. `tier1_queries.py` / `tier1w_writes.py` need to run
inside the container, so a replica you control gets the full three-instrument treatment that
a black-box hosted instance cannot.

Reference measurements against next.demo.nautobot.com (5 sequential samples, concurrency 1,
server time = time_starttransfer minus time_appconnect):

| endpoint | demo | here, unpatched | here, patched |
|---|---|---|---|
| `/api/dcim/devices/?limit=50` | 1051 ms | 231 ms | 174 ms |
| `/api/dcim/interfaces/?limit=100` | 947 ms | 373 ms | 141 ms |
| `/api/ipam/prefixes/?limit=100` | 274 ms | 58 ms | 55 ms |

The prefix row is the control: this branch barely changes that endpoint (58 -> 55ms), yet
the demo is 4.7x slower on it. That gap is environment, not code.
