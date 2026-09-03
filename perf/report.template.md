# Nautobot Performance Experiments

A measured account of Nautobot core's read and write paths, and the ranked findings that come
out of it. Every number here was produced by a reproducible harness against a fixed dataset.

<!--GEN:factbar-->

<!--GEN:provenance-->

## What was measured

An isolated stack on port 8180 with `DEBUG=False`, no Apps enabled, and pinned CPU and
memory, so results reflect production-shaped code paths and every finding is attributable to
core. The primary dataset is databot's `enterprise-campus / large / seed 42`: **24,091
objects** — 2,902 devices, 8,925 interfaces, 3,278 cables, 110 locations, 595 prefixes, 2,937
IP addresses and 36,552 existing ObjectChange records, applied over REST in 1,013 seconds.

Round one ran against `enterprise-campus / small / seed 42` (2,113 objects). Those numbers
are kept below as history, clearly separated, because they are the reference the first four
commits were measured against. Everything presented as current is large-dataset.

Four instruments, because the failure modes differ:

- **Query profiling** drives each scenario through the Django test client and counts SQL.
  Deterministic — it repeated **2,235 → 2,235** across runs, and reproduced exactly on a
  second machine with a different CPU architecture, which is what makes it usable as a
  regression gate.
- **Write-path profiling** runs each operation inside a real change context and a rolled-back
  transaction, so change logging and signals behave as they would for a live write.
- **Redis backend read counting**, because one endpoint was making more cache round-trips
  than database queries and no SQL-shaped tool could see it.
- **Wall-clock timing**, in-process and over HTTP, because some costs remove no queries at
  all.

The workload is hand-maintained rather than auto-generated. Endpoints are named by Django
view name and resolved through `reverse()`, and objects are selected by strategy at run time
— never a baked-in primary key — so the workload survives a reseed and fails loudly if a view
is renamed.

> ### The database is not the bottleneck
>
> Across a full baseline run, PostgreSQL executed **325,152 queries in 4.6 seconds total** — a
> mean of 0.014 ms each. The most-repeated query in the worst endpoint averaged **0.004 ms**.
> Every second of user-visible latency measured here is Python-side: ORM round-trip overhead,
> serialization, and repeated work per object.
>
> This has a practical consequence. Adding indexes would buy almost nothing at this scale, and
> the entire set of findings below is addressable in application code.
>
> The completed work confirms it from the other direction. Across the 38 scenarios, query
> count fell **57%** while total measured database time was **unchanged — 601 ms before,
> 602 ms after**. The 1,278 queries removed were worth almost nothing in SQL; what they cost
> was Python-side per-query overhead and the serialization work wrapped around them.

## Baseline — read path

Ten most expensive read scenarios of 38, on the large baseline (pristine tree, 24,091
objects). Duplicates are repeated query shapes after literal normalization.

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

## Baseline — write path

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
queries. Independently, `INSERT INTO extras_objectchange` is the single largest consumer of
database time across a whole run at **479 ms over 4,879 calls** — 10.4% of all time spent in
PostgreSQL. (That `pg_stat_statements` capture is from the round-one run and has not been
retaken at large scale; it is the one figure on this page whose dataset does not match the
tables around it.)

<!--GEN:findings-->

## Current state — all fourteen fixes at scale

Re-established against the 24,091-object dataset on a pristine tree, then measured with all
accepted fixes applied. Fourteen changes across 33 files, each with its own hypothesis, method
and numbers in its commit message.

Queries are exact and repeatable. In-process timing is the median of three separate runs of 15
repetitions.

| Scenario | Queries | In-process |
|---|---|---|
| **`api.interface.depth1`** | 1,229 → 292 (−76%) | 1,512 → 347ms (−77%) |
| `api.interface.list` | 23 → 23 | 373 → 153ms (−59%) |
| `api.device.list.depth1` | 112 → 11 (−90%) | 320 → 214ms (−33%) |
| `api.location.list` | 9 → 9 | 94 → 50ms (−47%) |
| `api.device.list` | 8 → 8 | 231 → 177ms (−23%) |
| `ui.device.detail` | 108 → 52 (−52%) | — |
| `ui.device.interfaces` | 71 → 39 (−45%) | — |
| `ui.location.detail` | 68 → 39 (−43%) | — |
| `ui.prefix.detail` | 79 → 49 (−38%) | — |
| `ui.rack.detail` | 99 → 63 (−36%) | — |
| `ui.interface.list` | 10 → 10 | 64 → 47ms (−27%) |
| **All 38 scenarios** | **2,235 → 957 (−57.2%)** | — |
| **Duplicate queries, all 38** | **1,604 → 346 (−78.4%)** | — |

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
reads instead of 3,600. Query count moves 9–13% because most of the remaining SQL is the
ObjectChange insert and the per-record `get_snapshots()` SELECT, neither of which any accepted
change touches.

Part of the write-path gain is inherited from the read path: change logging serializes through
the same API serializer, so prefetching nested natural keys cut `bulk.create.loop` from 1,604
to 1,404 queries before any write-specific change was made.

> **Correction.** The commit message for `5f351dc5b` credits serializer reuse with
> `bulk.update` 1,542 → 1,472 and `bulk.delete` 2,089 → 2,019. Those two query improvements
> were already present in the preceding run — the commit compared against a stale *before*
> file rather than the immediately preceding one. Its config-read result (98 → 2, 101 → 3)
> stands and is the change's real effect. No cumulative figure above is affected.

### Round-two reference, under uwsgi at concurrency 1

The absolute figures every future experiment is compared against, on the dedicated
measurement host. No before/after column: this is a reference, and it replaces a Tier 2
instrument that was measuring something else (see below).

<!--GEN:tier2-->

## What measurement overturned

**Query counts are page-bounded, not dataset-bounded.** At 11× the data the counts barely
moved, which retroactively validates the small dataset as an instrument for finding N+1s.

**Removing queries did not reduce database time.** 1,278 fewer queries, and total DB time
across the run went 601 ms to 602 ms. On this dataset the ORM round trip and the Python
wrapped around it are the cost; the SQL itself was never in the way.

**Three of my own conclusions were wrong and got corrected by measurement.** I claimed the
hierarchy endpoints scale with dataset size; page-size sensitivity testing showed that is true
for exactly one of five, and the UI list endpoints are entirely fixed cost. I put `nav_menu` at
40ms per request from a cProfile figure; true wall clock was ~16ms. And `ui.prefix.detail`
appeared to regress 11% until three repeat runs produced 329 / 447 / 347ms on identical code.

**Five hypotheses formed by reading code were wrong**, each corrected by instrumentation:
`Breadcrumbs.as_pair` was blamed for calling `.ancestors()` four times and calls it once;
`prepare_cloned_fields` was thought to run twice and runs once; an attribution of
`api.interface.depth1` named three small items and missed the item worth 70%; and the
ObjectChange double-serialization was assumed expensive on the redundant half, which is 12×
cheaper than the half that stays. Measure first; read code to explain a measurement, never to
predict one.

### Query count ranked the fixes in the wrong order

> **The single largest win removed zero queries.**
>
> Reusing nested serializers instead of rebuilding one per object is worth **−28.7%** on its
> own and changes no SQL at all. Memoizing the nav menu is worth **−24%** on detail pages and
> removes no query, no Redis read and no SQL — pure Python call overhead. On
> `api.interface.depth1`, a separately-measured change that removed 99 queries moved wall clock
> by **+0.3%**, and the rejected FK pre-warming removed 666 queries while running **62%
> slower**.
>
> Query count is the cheap, deterministic signal. It is the gate, not the objective.

### Four instruments, because each fix class is invisible to the others

| Instrument | Catches | Blind to |
|---|---|---|
| SQL query count | N+1s, missing prefetch | Python and Redis work |
| Redis backend reads | config and cache round-trip storms | SQL and pure CPU |
| Wall clock, in-process | everything Django does | the HTTP layer — reproduces to ±4% on a quiesced host |
| Wall clock, HTTP at concurrency 1 | everything, including HTTP and WSGI | nothing — reproduces to ±1.6%, and agrees with in-process to 1.08× |

The Redis counter exists because one endpoint was making **1,938 Redis round-trips against 977
SQL queries** — twice as many cache calls as database calls, entirely invisible to SQL-shaped
tooling.

Two rounds of alternating A/B were not enough twice over: a 2ms "regression" and an 11%
"regression" both dissolved on a third round. Four of the fourteen commits could quote no
wall-clock number at all, because the box was busy; their evidence is a deterministic counter
instead.

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
`ui.device.interfaces` reported a 66ms HTTP p95 against a 607ms in-process median. An API token
authenticates DRF only, so every UI endpoint had been answering **403** with a 299KB
permission-denied page that renders in ~50ms. cassowary counts an answered request as a
success, and the driver recorded no status code at all, so the run reported 38 endpoints and
zero failures while 27 of them timed an error page. Tier 2 now probes each endpoint once before
timing it, carries the status alongside the timing, and refuses to time anything that does not
answer 200.

Nor was the server the one anybody deploys. The dev image's CMD is `nautobot-server runserver`
— one threaded, GIL-bound process — while production runs uwsgi. Every round-one p95 described
a server nobody ships.

## What is next

### Remaining application-code targets

- **Row-scaling N+1s that memoization cannot help.** `PowerFeed.utilization` and the prefix
  hierarchy column both grow linearly with row count. Every fix on this branch so far removes
  *repeated* work; these need a different shape.
- **`get_snapshots()` issues one SELECT per ObjectChange** — 100 queries per 100-object bulk
  operation, and the largest remaining SQL item on the write path now that serialization is
  cached. Tracked upstream as issue #6303.
- **Residual `api.interface.depth1` cost is 292 queries**, down from 1,229. What is left is
  GenericForeignKey destination fetches and reverse one-to-one device-bay lookups on the nested
  Device serializer — the same mechanism as the accepted fixes, not yet applied to those paths.
- **`api.prefix.list` spends 13.7ms planning a query that executes in 1.3ms**, re-planned every
  request — about 21% of that endpoint's time. Explicitly not an index: an index makes planning
  worse. The lever is prepared-statement reuse or narrowing the serializer's `select_related`
  fan-out.

### Two things worth more than the next code fix

> **The largest measured gap is environment, not code.**
>
> Against `next.demo.nautobot.com` at the same API version: `/api/dcim/devices/?limit=50` takes
> 1,051ms there versus 231ms unpatched here, and `/api/ipam/prefixes/?limit=100` — an endpoint
> this branch barely changes, 58 → 55ms — takes 274ms, **4.7× slower**. That control isolates
> the difference as environment. TLS handshake was ~70ms, so it is not the network.
>
> One caveat on the magnitude: the "here" figures are in-process medians while the demo figures
> were taken over HTTPS against uwsgi, so the two sides used different instruments. Too large a
> gap to be all instrument, but it should be re-measured now that both sides can be taken at
> concurrency 1 under uwsgi.

**Coverage is ~5%.** 38 hand-picked scenarios against a surface of 330 API list endpoints, 320
detail endpoints and 127 UI list views. `api.interface.depth1` — now a 76% query reduction —
was found by guessing that interfaces is the biggest table. A screening pass over reads for
every model, normalized to cost per object, is designed in `perf/README.md` and not yet built.
It is a ranking instrument, not a gate; the inner loop stays at 38 scenarios.

## Caveats

- **The full test suite has not yet completed on this branch.** Targeted tests pass throughout
  — including `dcim.tests.test_api` (1,673) and `ipam.tests.test_views` (596) — but the one
  whole-suite attempt died in Django's parallel runner with a pickling error *and exited 0*.
  A serial run (`invoke tests --no-parallel`) is the gating item before any of this becomes a
  pull request. Note that `test_get_docs_url` fails across ~35 dcim model tests whenever
  `--skip-docs-build` is passed, on a clean tree too.
- **No changelog fragments and no docs yet.** 33 files of core changes, including a new public
  export (`nautobot.apps.tables.TemplateColumn`) that needs documenting and an upgrade note,
  since it reimplements django_tables2 3.0.1 internals. The fragments in `changes/` belong to
  unrelated develop work.
- **Absolute wall-clock figures are not comparable across the two machines used here.** Round
  one ran on an Apple Silicon workstation; round two on an Intel i5-8259U with turbo disabled,
  which is 4–6× slower in absolute terms and far more precise. Relative comparisons hold;
  absolute ones do not.
- **Fourteen accepted changes, ten of which introduce request-scoped state.** No single one is
  unjustified, and each is measured. The aggregate is still a lot of new caching for a reviewer
  to absorb at once, and it deserves to be read as a set rather than as fourteen unrelated
  diffs.
- **Improvements are not additive.** Several changes reduce natural-key work by different
  means, so their individual gains overlap rather than sum. Only the cumulative row is a sum.
- **Small-dataset history.** Round one measured 2,113 objects, where most list views paginate
  at 25–50 rows and look fast regardless. Those figures are kept for provenance, not for
  judging real-world value.
- **Silk middleware remains in the request chain.** It is inert without a session flag and
  constant across runs, so it does not distort relative comparisons, but it is present in every
  absolute number here.
- **UI response bodies are not byte-stable** between identical requests, so HTML endpoints are
  compared on query count and status rather than content.

---

<!--GEN:endnote-->
