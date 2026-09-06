# Nautobot Performance Experiments

A measured account of Nautobot core's read and write paths, and the ranked findings that come
out of it. Every number here was produced by a reproducible harness against a fixed dataset.

<!--GEN:factbar-->

<!--GEN:provenance-->

<!--GEN:findings-->

## Test environment and methodology

### The stack

An isolated stack on port 8180 with `DEBUG=False`, no Apps enabled (`PLUGINS = []`), and pinned
CPU and memory, so results reflect production-shaped code paths and every finding is
attributable to core. Served under uwsgi rather than `runserver`, because the dev image's CMD
is one threaded, GIL-bound process and production is not.

Those settings are not incidental. They live in `development/docker-compose.perf.yml` and
`development/nautobot_config.py`, and a checkout without them measures a different program:
`DEBUG=True` alone loads the debug toolbar, enables SQL logging, and has Django append every
query to `connection.queries` for the life of the process.

### The dataset

databot's `enterprise-campus / large / seed 42`: **24,091 objects** — 2,902 devices, 8,925
interfaces, 3,278 cables, 110 locations, 595 prefixes, 2,937 IP addresses and 36,552 existing
ObjectChange records, applied over REST in 1,013 seconds.

A `datacenter / large / seed 42` dataset (11,578 rows) is also kept, and is what the
whole-workflow write measurements use. Its shape is very different — patch-panel heavy, with
864 front ports and 864 rear ports that the campus dataset has none of — which is why per-model
costs transfer between the two but weightings do not.

Round one ran against `enterprise-campus / small / seed 42` (2,113 objects). Those figures are
history and are marked as such wherever they appear.

### The workload

Hand-maintained rather than auto-generated. Endpoints are named by Django view name and
resolved through `reverse()`, and objects are selected by strategy at run time — never a
baked-in primary key — so the workload survives a reseed and fails loudly if a view is renamed.

The inner loop is 38 read scenarios and 13 write operations. Two screening instruments cover
the surface the inner loop does not: `perf/screen_reads.py` enumerates every REST list endpoint
from the URL resolver and measures 518 read scenarios, and `perf/screen_writes.py` does the
same for the 152 endpoints that accept POST. Both normalize to cost per object, and both are
ranking instruments rather than gates.

### Four instruments, because each fix class is invisible to the others

| Instrument | Catches | Blind to |
|---|---|---|
| SQL query count | N+1s, missing prefetch | Python and Redis work |
| Redis backend reads | config and cache round-trip storms | SQL and pure CPU |
| Wall clock, in-process | everything Django does | the HTTP layer — reproduces to a median 1.7% spread across rounds, 7.4% worst case |
| Wall clock, HTTP at concurrency 1 | everything, including HTTP and WSGI | nothing — median 1.6% spread, 5.7% worst case, and agrees with in-process to 1.08× |

Query profiling drives each scenario through the Django test client and counts SQL. It is
deterministic — the same counts reproduced exactly on a second machine with a different CPU
architecture and host OS — which is what makes it usable as a regression gate. Write-path
profiling runs each operation inside a real change context and a rolled-back transaction, so
change logging and signals behave as they would for a live write.

The Redis counter exists because one endpoint was making **1,938 Redis round-trips against 977
SQL queries** — twice as many cache calls as database calls, entirely invisible to SQL-shaped
tooling.

Three alternating rounds is the minimum for any wall-clock claim. Two rounds were not enough
twice over: a 2 ms "regression" and an 11% "regression" both dissolved on a third round.

### Findings about the harness itself

<!--GEN:instruments-->

### Baseline — read path

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
are efficient at 9–10 queries each; the cost concentrates in nested API serialization and in
detail pages.

The last row is the outlier worth noting: `api.interface.list` issues only 23 queries yet was
the third-slowest endpoint on the branch — no N+1 at all, purely CPU-bound work per row. It is
the row that shaped the whole exercise, because no query-count instrument can see it.

### Baseline — write path

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

Two rows carry the story. `update.device.noop` — a save that changes no field — costs the same
35 queries as a real update, because change logging runs regardless. And the bulk triple
isolates what change logging costs: the same 100 rows take 1,604 queries with logging inline,
1,507 with logging deferred, and **3** with signals and validation bypassed entirely.

Deferral is the mechanism Nautobot's own bulk-edit views use, and it removes only 6% of
queries.

### Cumulative effect

<!--GEN:cumulative-->

Write path. Queries and config reads are deterministic; config reads are Redis round-trips
through Constance.

| Operation | Queries | Config reads | Wall |
|---|---|---|---|
| `bulk.create.x100.loop` | 1,604 → 1,404 (−12%) | 3,600 → 2 (−99.9%) | −28% |
| `bulk.create.x100.deferred` | 1,507 → 1,307 (−13%) | 3,600 → 2 (−99.9%) | −28% |
| `bulk.update.x100.deferred` | 1,675 → 1,472 (−12%) | 1,764 → 2 (−99.9%) | — |
| `bulk.delete.x100` | 2,222 → 2,019 (−9%) | 1,779 → 3 (−99.8%) | — |
| `create.interfaces.x50` | 804 → 704 (−12%) | 1,800 → 2 (−99.9%) | — |
| `create.interface` | 20 → 18 (−10%) | 36 → 2 | — |
| `create.device` | 34 → 34 | 15 → 3 | — |
| `bulk.create.x100.bulk_create` (floor) | 3 → 3 | 0 → 0 | — |

**Config reads are the signal on this path, not queries.** The natural-key lookup cache now
spans a whole transaction rather than one object, so a 100-object batch performs 2 Constance
reads instead of 3,600.

Part of the write-path gain is inherited from the read path: change logging serializes through
the same API serializer, so prefetching nested natural keys cut `bulk.create.loop` from 1,604
to 1,404 queries before any write-specific change was made.

**At whole-workflow scale the write path is worth −33.7%.** A complete `datacenter / large`
apply, measured on one box with the arms alternated and both trees proved different by content
hash before each run: stock `next` 1,967 s against this branch's 1,304 s, with queries
850,278 → 723,075 and server-side execution time 34,445 ms → 31,100 ms. Of the 663 seconds
saved, **3.3 are database execution** — the write-path win is Python, not SQL. See findings 39
and 40.

### Wall-clock references

The absolute figures every future experiment is compared against, on the dedicated measurement
host. No before/after column: these are references, and they replace a Tier 2 instrument that
was measuring something else (see the notes below).

**Under uwsgi at concurrency 1.**

<!--GEN:tier2-->

**In-process, same host**, with no HTTP layer, for the endpoints where serialization dominates.

<!--GEN:bench-->

## Notes

### The database is not the bottleneck

Across a full baseline run, PostgreSQL executed **325,152 queries in 4.6 seconds total** — a
mean of 0.014 ms each. The most-repeated query in the worst endpoint averaged **0.004 ms**.
Every second of user-visible latency measured here is Python-side: ORM round-trip overhead,
serialization, and repeated work per object.

This has a practical consequence. Adding indexes would buy almost nothing at this scale, and
the entire set of findings above is addressable in application code.

The completed work confirms it from the other direction, twice. On the read path, query count
fell **57%** across the 38 scenarios while total measured database time was **unchanged — 601
ms before, 602 ms after**. On the write path, a whole-workflow apply saved 663 seconds of which
3.3 were database execution. The queries removed were worth almost nothing in SQL; what they
cost was Python-side per-query overhead and the serialization work wrapped around them.

### Query count ranked the fixes in the wrong order

> **The single largest read-path win removed zero queries.**
>
> Reusing nested serializers instead of rebuilding one per object is worth **−28.7%** on its
> own and changes no SQL at all. Memoizing the nav menu is worth **−24%** on detail pages and
> removes no query, no Redis read and no SQL — pure Python call overhead. On
> `api.interface.depth1`, a separately-measured change that removed 99 queries moved wall clock
> by **+0.3%**, and the rejected FK pre-warming removed 666 queries while running **62%
> slower**.
>
> Query count is the cheap, deterministic signal. It is the gate, not the objective.

The write path is the same lesson at larger scale: −33.7% wall against −15.0% queries and
−9.7% database execution. Any future write experiment should report wall clock alongside query
count, or it will undervalue exactly the kind of fix this branch is best at.

### What measurement overturned

**Query counts are page-bounded, not dataset-bounded.** At 11× the data the counts barely
moved, which retroactively validates the small dataset as an instrument for finding N+1s.

**Three of my own conclusions were wrong and got corrected by measurement.** I claimed the
hierarchy endpoints scale with dataset size; page-size sensitivity testing showed that is true
for exactly one of five, and the UI list endpoints are entirely fixed cost. I put `nav_menu` at
40 ms per request from a cProfile figure; true wall clock was ~16 ms. And `ui.prefix.detail`
appeared to regress 11% until three repeat runs produced 329 / 447 / 347 ms on identical code.

**Five hypotheses formed by reading code were wrong**, each corrected by instrumentation:
`Breadcrumbs.as_pair` was blamed for calling `.ancestors()` four times and calls it once;
`prepare_cloned_fields` was thought to run twice and runs once; an attribution of
`api.interface.depth1` named three small items and missed the item worth 70%; and the
ObjectChange double-serialization was assumed expensive on the redundant half, which is the
cheaper half. Measure first; read code to explain a measurement, never to predict one.

**A commit message credited a change with improvements it did not make.** `5f351dc5b` credits
serializer reuse with `bulk.update` 1,542 → 1,472 and `bulk.delete` 2,089 → 2,019. Both were
already present in the preceding run — the commit compared against a stale *before* file rather
than the immediately preceding one. Its config-read result (98 → 2, 101 → 3) stands and is the
change's real effect, and no cumulative figure here is affected. Recorded because an A/B is only
as good as the file it compares against.

**A recorded prize is a measurement of a tree that no longer exists.** Finding 22 recorded
−14.2% on bulk create and was re-measured at −7.7% when it came to be implemented, because
finding 31 had since removed one of the two queries per record it was going to save. The branch
competed with itself and the ledger did not notice. Re-measure anything parked before
implementing it, not just before proposing it.

**A change declined on the axis you are chartered to measure is not a change that should not be
made.** That same finding was declined on write-path performance and landed on storage:
`object_data` is 11.8% of the changelog table, which matters wherever `CHANGELOG_RETENTION` is
long. The measurement was right and the recommendation was too narrow.

### The HTTP instrument was measured wrong twice before it was measured right

Tier 2 originally ran 30 requests at concurrency 4 and reported the 95th percentile. On a host
that holds in-process timings to ±4%, that configuration spread **25.3% at the median and
41.4% at worst** across three rounds of identical code. The p95 was drawn from roughly two
samples in the tail, against three uwsgi workers sharing two physical cores, in integer
milliseconds. It was noise wearing a percentile.

At concurrency 1 reporting the median, the same 38 endpoints on the same code spread **1.6% at
the median and 5.7% at worst** — a sixteenfold improvement in precision, from changing the
instrument rather than the hardware.

The result that matters is not the precision. It is that the two independent instruments now
**corroborate** each other: HTTP at concurrency 1 runs a median **1.08×** in-process across
fourteen shared endpoints, range 1.04–1.19×, which is what HTTP parsing, WSGI and response
transfer should cost on top of identical Django work. Under the old configuration they
disagreed by 4–6× and nothing could say which was right.

Before that they disagreed in a way that was physically impossible, and it went unnoticed:
`ui.device.interfaces` reported a 66 ms HTTP p95 against a 607 ms in-process median. An API
token authenticates DRF only, so every UI endpoint had been answering **403** with a 299 KB
permission-denied page that renders in ~50 ms. cassowary counts an answered request as a
success, and the driver recorded no status code at all, so the run reported 38 endpoints and
zero failures while 27 of them timed an error page. Tier 2 now probes each endpoint once before
timing it, carries the status alongside the timing, and refuses to time anything that does not
answer 200.

### What is still open

The live queue is `perf/queue.md`, which carries the ordering and the reasoning behind it. The
current head of it:

- **`dcim.cable` is the most expensive create on the write surface**, at roughly **220 queries
  and 0.34 seconds per cable**, linear from 25 to 96 per request. It is also the worst model on
  the read side: `dcim.cable?depth=1` issues 159 queries for a page of 25, of which 145 are
  repeats of a shape already seen, and 50 of those are one `SELECT` on `dcim_device` — two per
  cable, one per termination's parent. Depth 0 is 7 queries with no duplicates, so all of it is
  nested serialization. Known shape, known mechanism, not yet attributed to a fix.
- **`ipam.ipaddresstointerface` at 65.6 marginal queries per created object**, the top of the
  write ranking, on a 2,937-row table.
- **Row-scaling N+1s that memoization cannot help.** `PowerFeed.utilization` and the prefix
  hierarchy column both grow linearly with row count. Every fix on this branch removes
  *repeated* work; these need a different shape.
- **`api.prefix.list` spends 13.7 ms planning a query that executes in 1.3 ms**, re-planned
  every request — about 21% of that endpoint's time. Explicitly not an index: an index makes
  planning worse. The lever is prepared-statement reuse or narrowing the serializer's
  `select_related` fan-out.
- **An affordance-adoption screen has not been built.** Three findings on this branch exist
  only because an affordance was added without its call sites being updated to use it. Screening
  endpoints by cost finds symptoms; screening affordances by adoption finds causes, over a much
  smaller search space.

> **The largest measured gap is environment, not code.**
>
> Against `next.demo.nautobot.com` at the same API version: `/api/dcim/devices/?limit=50` takes
> 1,051 ms there versus 231 ms unpatched here, and `/api/ipam/prefixes/?limit=100` — an endpoint
> this branch barely changes, 58 → 55 ms — takes 274 ms, **4.7× slower**. That control isolates
> the difference as environment. TLS handshake was ~70 ms, so it is not the network.
>
> One caveat on the magnitude: the "here" figures are in-process medians while the demo figures
> were taken over HTTPS against uwsgi, so the two sides used different instruments. Too large a
> gap to be all instrument, but it should be re-measured now that both sides can be taken at
> concurrency 1 under uwsgi.

### Caveats

- **Test coverage of the tree as it stands is partial, and this states which part.** A whole-suite
  run — `invoke tests --no-parallel --no-keepdb --no-input`, all **17,504 tests in 2h 18m** —
  passed against `fd48eee32`: OK, 663 skipped, 1 expected failure, zero failures and zero errors.
  Findings 22 and 37–40 landed after it. Finding 22 is the only one of those that changes product
  code, and it was covered by `nautobot.extras` in full (**4,976 tests, OK**) plus
  `core.tests.test_utils`, `core.tests.test_graphql`, `users.tests.test_filters` and
  `ipam.tests.migration.test_migrations`. A whole-suite run has not been repeated since.
- **No changelog fragments, deliberately.** Nothing here reaches a release in its current shape:
  anything upstreamed would go through review and very likely change, so a fragment written now
  would describe a change that no longer exists. The commit messages and the findings records
  carry more than a fragment would, and each finding's caveat is already written the way a
  release note would have to write it.
- **Absolute wall-clock figures are not comparable across machines.** Round one ran on an Apple
  Silicon workstation; round two on an Intel i5-8259U with turbo disabled, which is 4–6× slower
  in absolute terms and far more precise. Relative comparisons hold; absolute ones do not. The
  same applies across datasets: figures taken against `enterprise-campus` and against
  `datacenter` are not interchangeable.
- **Twenty-one accepted changes to Nautobot, many of which introduce request-scoped state.** No
  single one is unjustified, and each is measured. The aggregate is still a lot of new caching
  for a reviewer to absorb at once, and it deserves to be read as a set.
- **Improvements are not additive.** Several changes reduce natural-key work by different means,
  so their individual gains overlap rather than sum. Only the cumulative row is a sum.
- **The write screen measures a floor, not a cost.** `perf/screen_writes.py` populates required
  fields only, so any model whose expensive work hangs off an *optional* relation is understated
  by an unknown factor. `dcim.cable` is the known case: reported at 44.9 marginal queries per
  object with both terminations null, against ~220 for a cable that is actually connected.
- **Silk middleware remains in the request chain.** It is inert without a session flag and
  constant across runs, so it does not distort relative comparisons, but it is present in every
  absolute number here.
- **UI response bodies are not byte-stable** between identical requests, so HTML endpoints are
  compared on query count and status rather than content.

---

<!--GEN:endnote-->
