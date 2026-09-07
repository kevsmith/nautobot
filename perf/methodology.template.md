# Nautobot performance experiments: methodology and evidence

Every number in [`perf/report.md`](report.md), with the instrument that produced it and the
working behind each finding.

<!--GEN:provenance-->

## The instruments, and a warning about the word "tier"

Two unrelated taxonomies share the word. Risk tiers (`A`, `B1`, `B2`, `C`, `M0`-`M2`) price
what a change costs to adopt, and are what the report's Tiers table describes. Instrument
tiers (Tier 1, Tier 1W, Tier 2) are harness scripts, numbered in the order they were built,
with no hierarchy of authority implied. Prefer the names below, which say what each one
measures.

| Name | Script | Measures | Cost |
|---|---|---|---|
| Read query profile (Tier 1) | `tier1_queries.py` | SQL per read scenario, in-process | seconds, deterministic |
| Write query profile (Tier 1W) | `tier1w_writes.py` | SQL per write operation, in a rolled-back transaction | seconds, deterministic |
| HTTP latency (Tier 2) | `tier2_latency.py` | wall clock over real HTTP against uwsgi | minutes, needs a quiet host |
| In-process latency | `bench_endpoints.py` | wall clock with no HTTP layer | minutes, needs a quiet host |
| Read screen | `screen_reads.py` | every REST list endpoint, ranked per object | minutes |
| Write screen | `screen_writes.py` | every POST endpoint, ranked per created object | minutes |

Tier 2's docstring still describes it as the slow, noisy confirmation of a Tier 1 result. At
concurrency 1 it holds a 1.6% median spread and agrees with in-process timing to 1.08x, which
makes it a peer instrument. The scripts keep their names because dozens of finding records cite
them as historical fact, and renaming the committed baseline files would break the provenance
those records depend on. A rename is on the queue.

## Test environment

### The stack

An isolated stack on port 8180 with `DEBUG=False`, no Apps enabled (`PLUGINS = []`), and pinned
CPU and memory, so results reflect production-shaped code paths and every finding is
attributable to core. Served under uwsgi rather than `runserver`, because the dev image's CMD
is one threaded, GIL-bound process and production is not.

Those settings are not incidental. They live in `development/docker-compose.perf.yml` and
`development/nautobot_config.py`, and a checkout without them measures a different program.
`DEBUG=True` alone loads the debug toolbar, enables SQL logging, and has Django append every
query to `connection.queries` for the life of the process.

### The dataset

databot's `enterprise-campus / large / seed 42`: **24,091 objects**, comprising 2,902 devices,
8,925 interfaces, 3,278 cables, 110 locations, 595 prefixes, 2,937 IP addresses and 36,552
existing ObjectChange records, applied over REST in 1,013 seconds.

A `datacenter / large / seed 42` dataset (11,578 rows) is also kept, and is what the
whole-workflow write measurements use. Its shape is very different, patch-panel heavy, with
864 front ports and 864 rear ports that the campus dataset has none of, which is why per-model
costs transfer between the two but weightings do not.

Round one ran against `enterprise-campus / small / seed 42` (2,113 objects). Those figures are
history and are marked as such wherever they appear.

### The workload

Hand-maintained rather than auto-generated. Endpoints are named by Django view name and
resolved through `reverse()`, and objects are selected by strategy at run time, never a
baked-in primary key, so the workload survives a reseed and fails loudly if a view is renamed.

The inner loop is 38 read scenarios and 13 write operations. Two screening instruments cover
the surface the inner loop does not: `perf/scripts/screen_reads.py` enumerates every REST list
endpoint from the URL resolver and measures 518 read scenarios, and
`perf/scripts/screen_writes.py` does the same for the 152 endpoints that accept POST. Both
normalize to cost per object, and both rank rather than gate.

### Each fix class is invisible to the other instruments

| Instrument | Catches | Blind to |
|---|---|---|
| SQL query count | N+1s, missing prefetch | Python and Redis work |
| Redis backend reads | config and cache round-trip storms | SQL and pure CPU |
| Wall clock, in-process | everything Django does | the HTTP layer; reproduces to a median 1.7% spread across rounds, 7.4% worst case |
| Wall clock, HTTP at concurrency 1 | everything, including HTTP and WSGI | nothing; median 1.6% spread, 5.7% worst case, and agrees with in-process to 1.08x |

Query profiling drives each scenario through the Django test client and counts SQL. It is
deterministic: the same counts reproduced exactly on a second machine with a different CPU
architecture and host OS, which is what makes it usable as a regression gate. Write-path
profiling runs each operation inside a real change context and a rolled-back transaction, so
change logging and signals behave as they would for a live write.

The Redis counter exists because one endpoint was making **1,938 Redis round-trips against 977
SQL queries**, twice as many cache calls as database calls, entirely invisible to SQL-shaped
tooling.

Three alternating rounds is the minimum for any wall-clock claim. Two rounds were not enough
twice over: a 2 ms "regression" and an 11% "regression" both dissolved on a third round.

## Baseline: read path

Ten most expensive read scenarios of 38, on the large baseline (pristine tree, 24,091 objects).
Duplicates are repeated query shapes after literal normalization.

| Scenario | Queries | Duplicate |
|---|---:|---:|
| `api.interface.depth1` | 1,229 | 1,198 |
| `api.device.list.depth1` | 112 | 101 |
| `ui.device.detail` | 108 | 55 |
| `ui.device.detail.worst` | 104 | 49 |
| `ui.rack.detail` | 99 | 45 |
| `ui.home` | 79 | 32 |
| `ui.prefix.detail` | 79 | 34 |
| `ui.device.interfaces` | 71 | 34 |
| `ui.location.detail` | 68 | 27 |
| `api.interface.list` | 23 | 3 |

All 38 endpoints returned 200, and every query count repeated exactly across runs. List views
are efficient at 9-10 queries each; the cost concentrates in nested API serialization and in
detail pages.

`api.interface.list` issues 23 queries and was still the third-slowest endpoint on the branch:
no N+1, purely CPU-bound work per row. No query-count instrument can see that cost, which is
what shaped the rest of the exercise.

## Baseline: write path

Each operation measured inside a change context and rolled back, on the large baseline.
"Changes" counts ObjectChange records created.

| Operation | Queries | Wall | Changes |
|---|---:|---:|---:|
| `create.device` | 34 | 19.6 ms | 1 |
| `update.device.name` | 35 | 19.9 ms | 1 |
| **`update.device.noop`** | **35** | 19.9 ms | 1 |
| `delete.device` | 174 | 68.2 ms | 2 |
| `create.interface` | 20 | 15.1 ms | 1 |
| `create.ipaddress` | 22 | 12.9 ms | 1 |
| `create.tag` | 10 | 8.3 ms | 1 |
| `bulk.create.x100.loop` | 1,604 | 1,255 ms | 100 |
| `bulk.create.x100.deferred` | 1,507 | 1,115 ms | 100 |
| **`bulk.create.x100.bulk_create`** | **3** | 9.4 ms | 0 |
| `bulk.update.x100.deferred` | 1,675 | 1,152 ms | 49 |
| `bulk.delete.x100` | 2,222 | 1,191 ms | 50 |

`update.device.noop`, a save that changes no field, costs the same 35 queries as a real update,
because change logging runs regardless. The bulk triple isolates what change logging costs: the
same 100 rows take 1,604 queries with logging inline, 1,507 with logging deferred, and **3**
with signals and validation bypassed entirely.

Deferral is the mechanism Nautobot's own bulk-edit views use, and it removes only 6% of
queries.

## Wall-clock references

The absolute figures every future experiment is compared against, on the dedicated measurement
host. No before/after column: these are references, and they replace a Tier 2 instrument that
was measuring something else.

**Under uwsgi at concurrency 1.**

<!--GEN:tier2-->

**In-process, same host**, with no HTTP layer, for the endpoints where serialization dominates.

<!--GEN:bench-->

## Findings about the harness itself

<!--GEN:instruments-->

## Per-finding evidence

For each change in the report that carries working: how its wall clock was taken, what was held
flat as a control, which tests ran, and what the finding concluded beyond its own verdict.
Numbered to match the report.

<!--GEN:evidence-->

## Corrections forced by measurement

**Query counts are bounded by page size rather than dataset size.** At 11x the data the counts
barely moved, which retroactively validates the small dataset as an instrument for finding
N+1s.

**Measurement corrected conclusions I had already drawn.** I claimed the hierarchy endpoints
scale with dataset size; page-size sensitivity testing showed that is true for exactly one of
five, and the UI list endpoints are entirely fixed cost. I put `nav_menu` at 40 ms per request
from a cProfile figure; true wall clock was ~16 ms. And `ui.prefix.detail` appeared to regress
11% until three repeat runs produced 329 / 447 / 347 ms on identical code.

**Hypotheses formed by reading code were wrong**, each corrected by instrumentation:
`Breadcrumbs.as_pair` was blamed for calling `.ancestors()` four times and calls it once;
`prepare_cloned_fields` was thought to run twice and runs once; an attribution of
`api.interface.depth1` named three small items and missed the item worth 70%; and the
ObjectChange double-serialization was assumed expensive on the redundant half, which is the
cheaper half. Reading code proved reliable for explaining a measurement and unreliable for
predicting one.

**A commit message credited a change with improvements it did not make.** `5f351dc5b` credits
serializer reuse with `bulk.update` 1,542 -> 1,472 and `bulk.delete` 2,089 -> 2,019. Both were
already present in the preceding run, because the commit compared against a stale *before* file
rather than the immediately preceding one. Its config-read result (98 -> 2, 101 -> 3) stands and
is the change's real effect, and no cumulative figure is affected. Recorded because an A/B
result depends on which *before* file it was compared against.

**A recorded prize is a measurement of a tree that no longer exists.** Finding 22 recorded
-14.2% on bulk create and was re-measured at -7.7% when it came to be implemented, because
finding 31 had since removed one of the two queries per record it was going to save. Nothing in
the ledger flagged the overlap. Re-measure anything deferred at the point of implementing it,
not only at the point of proposing it.

**A change can fail on performance and still be worth making.** That same finding was declined
on write-path performance and landed on storage: `object_data` is 11.8% of the changelog table,
which matters wherever `CHANGELOG_RETENTION` is long. The measurement was right; the
recommendation drawn from it was too narrow.

## Getting the HTTP instrument right

Tier 2 originally ran 30 requests at concurrency 4 and reported the 95th percentile. On a host
that holds in-process timings to +/-4%, that configuration spread **25.3% at the median and
41.4% at worst** across three rounds of identical code. The p95 was drawn from roughly two
samples in the tail, against three uwsgi workers sharing two physical cores, in integer
milliseconds.

At concurrency 1 reporting the median, the same 38 endpoints on the same code spread **1.6% at
the median and 5.7% at worst**, a sixteenfold improvement in precision from changing the
instrument, with no change to the hardware.

The two independent instruments now agree. HTTP at concurrency 1 runs a median **1.08x**
in-process across fourteen shared endpoints, range 1.04-1.19x, which is what HTTP parsing, WSGI
and response transfer should cost on top of identical Django work. Under the old configuration
they disagreed by 4-6x and nothing could say which was right.

Earlier still, `ui.device.interfaces` reported a 66 ms HTTP p95 against a 607 ms in-process
median. An API token authenticates DRF only, so every UI endpoint had been answering **403**
with a 299 KB permission-denied page that renders in ~50 ms. cassowary counts an answered
request as a success, and the driver recorded no status code at all, so the run reported 38
endpoints and zero failures while 27 of them timed an error page. Tier 2 now probes each
endpoint once before timing it, carries the status alongside the timing, and refuses to time
anything that does not answer 200.

## Still open

The live queue is `perf/queue.md`, which carries the ordering and the reasoning behind it. The
current head of it:

- `dcim.cable` is the most expensive create on the write surface, at roughly **220 queries and
  0.34 seconds per cable**, linear from 25 to 96 per request. It is also the worst model on the
  read side: `dcim.cable?depth=1` issues 159 queries for a page of 25, of which 145 are repeats
  of a shape already seen, and 50 of those are one `SELECT` on `dcim_device`, two per cable, one
  per termination's parent. Depth 0 is 7 queries with no duplicates, so all of it is nested
  serialization. Known shape, known mechanism, not yet attributed to a fix.
- `ipam.ipaddresstointerface` at 65.6 marginal queries per created object, the top of the write
  ranking, on a 2,937-row table.
- Row-scaling N+1s that memoization cannot help. `PowerFeed.utilization` and the prefix
  hierarchy column both grow linearly with row count. Every fix on this branch removes
  *repeated* work; these need a different shape.
- `api.prefix.list` spends 13.7 ms planning a query that executes in 1.3 ms, re-planned every
  request, about 21% of that endpoint's time. Explicitly not an index: an index makes planning
  worse. The lever is prepared-statement reuse or narrowing the serializer's `select_related`
  fan-out.
- An affordance-adoption screen has not been built. Five findings on this branch exist only
  because an affordance was added without its call sites being updated to use it. Screening
  endpoints by cost finds symptoms; screening affordances by adoption finds causes, over a much
  smaller search space.
- No stock-vs-branch aggregate read-path wall clock has been taken. Round one and round two ran
  on different hosts, so the only cumulative read-path figure that survives the host change is a
  query count. The measurement is affordable on the current host and has not been run.
- The demo-instance gap needs re-measuring with one instrument. The 4.7x figure in the report
  compares in-process medians here against HTTPS-to-uwsgi there. Both sides can now be taken at
  concurrency 1 under uwsgi.

## Caveats

- Test coverage is complete for the tree these numbers describe. A whole-suite run of **17,505
  tests in 8,151 s** passed against the exact tree every measurement below was taken against:
  `OK (skipped=663, expected failures=1)`, zero failures, zero errors, product tree content hash
  `90dbd96ffbeae6d1b9ed83b3e1042af10f95cdcc6fcc19e4a734be0d30ff36bf`. The hash is quoted rather
  than a commit because that is what `arm_control.sh` proves before each arm, so the suite and
  the measurements are attached to the same artefact rather than to a branch name that moves.
  The tree has since gained two test modules, which changes that hash, since test files live
  under `nautobot/` and `arm_control.sh` hashes every `.py` under it. Nothing measured here
  moved: the difference is test code only, and it was added after every figure above was taken.
- The new request-scoped state is now tested, and the third-party coupling fails loudly. It was
  neither before. `CachingTemplateColumnCouplingTestCase` renders the same cell through the
  caching column and through django-tables2's stock `TemplateColumn` and asserts the output is
  identical, so an upgrade that changes `render()` semantics breaks a test rather than producing
  wrong cell content. Writing it surfaced a coupling nobody had recorded: the reimplementation
  depends on `kwargs["bound_row"]` reaching `get_context_data()`, not only on that hook
  existing, so there is now an explicit test for the kwarg contract.
  `NaturalKeyFieldLookupsCacheScopeTestCase` covers the other half: the cache is absent outside
  its scope, present inside, removed on the exception path, reused rather than shadowed when
  nested, and returns the same answer as the uncached path for Device and Location. 11 tests,
  all passing. The aggregate is still a lot of new caching for one review, and it still deserves
  to be read as a set.
- Absolute wall-clock figures are not comparable across machines or datasets. Round one ran on
  Apple Silicon, round two on an Intel i5-8259U with turbo disabled, 4-6x slower and far more
  precise. Relative comparisons hold; absolute ones do not.

## Further caveats

- The write screen measures a floor, and the factor is now bounded for most of the surface.
  `perf/scripts/screen_writes.py` populates required fields only. Re-running it with
  `--include-optional` and comparing 73 models measured both ways puts the median understatement
  at **1.00x**, so for the typical model the floor is the cost, with a mean of 1.31x and a
  maximum of 4.96x; 10 of 73 exceed 1.5x. **What it cannot bound is the part that matters**: 27
  models could not be measured with populated payloads at all, including `dcim.cable`,
  `dcim.interface` and `dcim.powerfeed`, because a model with expensive optional relations is
  both the most understated and the hardest to build a payload for. `dcim.cable` is quantified
  by another route: finding 40 measured a connected cable at ~220 queries against the floor's
  44.9, which is 4.9x and consistent with the top of the measured range. See finding 47.
- Silk middleware remains in the request chain. It is inert without a session flag and constant
  across runs, so it does not distort relative comparisons, but it is present in every absolute
  number here.
- UI response bodies are not byte-stable between identical requests, so HTML endpoints are
  compared on query count and status rather than content.

---

<!--GEN:endnote-->
