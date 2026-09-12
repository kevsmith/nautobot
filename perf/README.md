# Nautobot performance harness

This file is the authority on *what* is measured and *why*: scope, gate semantics, the risk
taxonomy, the optimization loop. **`perf/scripts/README.md` is the authority on *how*** — what
each script does, which ones compose into a recipe, and the operational traps that have cost
real time. Start here, then read that before running anything.

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

**On the rolled-back transactions.** Measured, because the box being dedicated
makes restore-based isolation affordable and the question deserved a number:
committing rather than rolling back `bulk.create.x100.deferred` costs **−0.9%**,
inside variance, over three rounds each way with a restore before every arm.
Rollback's figures stand, and it stays because it keeps Tier 1W fast enough for
the inner loop. What it omits is qualitative -- `transaction.on_commit`
callbacks never fire, and Nautobot uses them in five places -- so an experiment
that changed commit-time behaviour would measure zero under it. See finding 29.

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
docker compose exec nautobot python /source/perf/scripts/tier1_queries.py \
    --out /source/perf/baselines/tier1-baseline.json \
    --dump-urls /source/perf/results/urls.json
python3 perf/scripts/tier2_latency.py --urls perf/results/urls.json \
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
python3 perf/scripts/compare.py --baseline perf/baselines/tier1-baseline.json \
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

**No changelog fragments on this branch.** Nothing here reaches a release in its
current shape, so a fragment written now would describe a change that review has
not touched yet. The commit message carries the hypothesis, method, numbers and
risk tier; the findings record carries the caveat in release-note form. Between
them there is nothing a fragment would add. Fragments and user-facing docs get
written against whatever shape survives review, by whoever lands it.

## The gate comes before the taxonomy

A change must **be faster** and must not break availability.

**Availability is the hard gate.** An endpoint that returned 200/302 at baseline
must not start erroring or 404ing. Content and row ordering may change and are
reported for information only. There is no arguing with this one.

**A query-count regression is a tripwire, not a verdict.** `compare.py` exits 1
on any increase, and that is the right default -- it makes an increase
impossible to miss. But this branch has spent its whole length establishing that
query count misranks changes, and that cuts both ways: a change that removed 666
queries was rejected for running 62% slower, and a change that adds 4 queries to
one endpoint while removing 162 from another is not obviously bad.

So an exit 1 means *explain it*, with wall clock measured on both the endpoint
that improved and the one that regressed. Then either take the change and record
the trade, or don't. What is not acceptable is reverting on the sign of an
integer, or shipping without measuring what the regression costs. Batch queries
are the specific trap: a query that fetches 83 rows is not interchangeable with
a point lookup, so per-query heuristics do not transfer to them.

- The improvement is real on an instrument that can see it, with the arms proven
  to differ by a deterministic counter.
- Wall clock takes three alternating rounds, and the arm order **reverses between
  rounds**. Running the same arm first every time lets a monotonic drift bias one
  arm: on finding 26 two endpoints the change could not touch read +7.1% and
  +4.9% for the arm that always went first, and had to be discarded. A figure
  that moves against the ordering bias is the one worth trusting.

**Every experiment measures wall clock**, whatever its primary signal is. That was not
true for the first fifteen: eight carried no wall-clock figure, because timings on the
original machine varied by the same order as the effects being measured. The dedicated
measurement host reproduces in-process timings to a median 1.7% spread across rounds
(7.4% worst case) and HTTP at concurrency 1 to 1.6% (5.7% worst), so the reason to skip it
is gone -- and it is the first number a reader looks for. A
query-count win that does not move wall clock is a result in its own right.

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
- **`third-party-coupled`** -- reimplements or depends on internals of a
  dependency, so an upgrade can change behaviour rather than break a signature.
  The caching `TemplateColumn` reimplements django_tables2 3.0.1's `render()`
  and is the case in hand. It no longer fails silently:
  `CachingTemplateColumnCouplingTestCase` renders the same cell through both
  implementations and asserts they match, so a divergence fails a test. Writing
  that test found a second coupling nobody had recorded -- the reimplementation
  needs `kwargs["bound_row"]` to reach `get_context_data()`, not just the hook
  to exist, and asserting `hasattr` would have passed while the code broke.

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

## The report is verified, not just generated

`build_report.py --check` proves the report was rendered from the current
sources. It does not prove the sources agree with each other.
`perf/scripts/verify_report.py` does, in 1,385 checks:

- every commit SHA in the accepted table resolves on the branch the report names
- no finding points at a commit that was later reverted
- no committed baseline holds a record that cannot be a measurement -- status
  200, a non-empty body, zero queries
- every figure in the cumulative table recomputes from the file it came from,
  and anything marked stale says so in the report
- every Reason cell is a finding's own words rather than the report's

Each of those exists because something was wrong once. A reverted commit whose
subject says "BREAKS 13 TESTS" was published as an accepted change. A query
counter returned zero on status-200 responses with full bodies. An aggregate was
quoted for weeks against a tree three findings behind the branch. Generation
protects against drift between a template and its data; none of it protects
against data that is wrong.

## The workload schema, and three fields that exist for one reason each

`perf/workload.yml` scenarios are `{id, view, query, pick, tags}` by default, and
a URL always comes from `reverse()` so a renamed view fails loudly rather than
silently measuring a redirect. Three fields opt out of parts of that, and each
was added because a measurement was wrong without it.

**`headers`** — a Nautobot list view builds its table over `queryset.none()`
unless the request carries `HX-Request` (`core/views/renderers.py:96`), and the
browser fetches the real table on a follow-up fired by `hx-trigger="load"`. Every
`ui.*.list` scenario was therefore measuring a page that rendered no rows. The
18 `ui.*.list.rows` twins carry the header; both halves are kept, because a page
load pays for both and dropping either just moves the blind spot. See finding 44.

**`expected_status`** — a control scenario is allowed to fail on purpose.
`ui.chrome.404` measures what every page costs before it renders anything of its
own: `404.html` extends `40x.html` extends `base.html`, so it renders the full
chrome with a static card for content. Without this field the harness flags it
and Tier 2 refuses to time it.

**`path`** — a literal URL, used only by that 404 control, because a 404 has no
view to reverse. Declaring it opts out of the reverse-only rule explicitly
rather than by accident.

## Two guards on the query counter

**The log is cleared before every capture.** `CaptureQueriesContext` slices
`len(connection.queries_log)` between entry and exit, and that log is a
`deque(maxlen=9000)` shared for the life of the process. A long run saturates it,
the length stops growing, and every subsequent capture reports zero — silently,
on a status-200 response with a full body and `query_count_stable: True`. Worse,
it degrades before it fails: one endpoint reported 495 of its 1,067 queries while
looking entirely ordinary. It bit the *stock* arm of a 57-scenario run and not the
branch arm, because the slower arm saturates first, so the bias always favours the
branch. `queries_limit` is also raised to 18,000 for the residual case of a single
request exceeding the limit — `bulk.delete.x100` is already 2,222 queries. See
finding 45.

**An implausible record fails the run.** No authenticated Django page serves a
200 with a body in zero queries; there is a session lookup and a user fetch before
the view runs. `tier1_queries.py` flags that combination and `compare.py` refuses
to compare against it. This is the guard that generalises: a stability check
compares reps against each other, so three reps agreeing on a wrong answer reads
as confidence. Only a plausibility check catches a systematic failure.

## Findings are structured data, and the reports are generated

`perf/findings/*.yml` is the source of record for every experiment: its tier,
flags, caveat, instruments, controls, tests and status. `perf/scripts/build_report.py`
renders two documents from those files plus the committed baselines, filling the
`<!--GEN:...-->` markers in each template. Narrative prose stays in the
templates, where writing it by hand is the point.

- `perf/report.md` -- what was found and what it is worth. One entry per change:
  the defect, the instruments, the caveat. Nothing about how the number was
  taken.
- `perf/methodology.md` -- why the numbers are believable. Instruments,
  baselines, wall-clock references, the harness findings, and the per-finding
  working: wall-clock detail, controls, tests, notes.

Each finding's `basis` is the one-cell answer to "why was this accepted or
rejected", and it defaults to `wall_clock` because that is the reason for most
of them. It is written explicitly where it is not: finding 42 was taken for
consistency at -0.2%, finding 22 for storage, finding 23 refused on
correctness. A "not measured" cell in that column said nothing about why anyone
kept the change.

The split is not cosmetic. With both in one file, 55% of a 155KB report was
per-finding evidence, and the thing the report exists to present -- a ranked list
of working optimizations with their measured value -- was buried in the working
that produced it. A finding's `note`, `controls`, `tests` and
`wall_clock_detail` render only in `methodology.md`; everything else renders in
both places from the same YAML, so neither can drift from the other.

    python3 perf/scripts/build_report.py            # write both documents
    python3 perf/scripts/build_report.py --check    # exit 1 if either has drifted

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

## Closed: the full test suite passes

`invoke tests --parallel-workers=1 -n -k --no-cache-test-fixtures` ran the whole
suite on 2026-09-12 against `perf/recommended` at `977eb44c6`, the tree a
reviewer would take:

    Ran 17533 tests in 7626.657s
    OK (skipped=663, expected failures=1)

Zero failures, zero errors, and no `test_get_docs_url` at all -- the run builds
docs rather than passing `--skip-docs-build`, which is what makes those ~35 dcim
model tests fail even on a clean tree. `perf/recommended` and `perf/experiments`
have byte-identical `nautobot/` trees, so the result covers both.

The collected count is 29 higher than the 17,504 recorded at `fd48eee32`, which
is the check that matters as much as the failure count: an import error shrinks
the suite without failing anything.

Django's parallel runner cannot run this suite. It dies during subsuite setup
with a pickling error that hides the underlying exception, prints no test names,
and **exits 0**. It reproduces at `cc45a35f5`, so it is not this branch's doing,
and `--parallel-workers=1` is the only invocation that produces a trustworthy
result.

Two things to carry forward. Fixture caching was off this time, so the cached
fixture is ruled out as a source of stale state, but `--keepdb` stayed on and the
log records `Using existing test database for alias 'default'` -- a full
`--no-keepdb` run is still untried, and needs `--no-input` beside it or it blocks
on a prompt. And it took 2h11m on the measurement host, which is a background
job rather than an inner-loop check -- targeted per-module runs stay the fast
signal during an experiment.

## Built: a one-second database reset

`perf/scripts/reset_db.sh` returns the database to baseline by cloning a pristine template
(`CREATE DATABASE ... TEMPLATE`) rather than replaying the snapshot. 1.3 seconds
against `restore_snapshot.sh`'s 49, with the app left running -- `DROP DATABASE ...
WITH (FORCE)` evicts the pooled connections and Django reconnects on the next
request. celery survives it too.

    perf/scripts/restore_snapshot.sh        # establish baseline (the authority, 49s)
    perf/scripts/reset_db.sh --build        # build the template from it (one time, 3.7s)
    perf/scripts/reset_db.sh                # reset (1.3s) -- as often as you like

Two things this buys and one it costs.

It makes per-operation isolation affordable, which is what the write matrix needs.
It also re-prices finding 29, which declined restore-based isolation for Tier 1W
partly because it "would add ~70 seconds per arm" -- a figure taken from the slow
path. The finding still stands, but on its other reason: commit against rollback
is -0.9%, inside variance, so there is nothing to gain by switching.

The cost is one request. The clone's files are cold in PostgreSQL's shared buffers,
so the first request after a reset ran 981.8ms median against a 706.4ms warm
control, and varied 719.6 / 981.8 / 1138.9ms across rounds. The second request is
already indistinguishable from warm. **Discard exactly one request after a reset.**
Without that, every model in the write matrix carries a variable few-hundred-
millisecond bias, in the instrument built to make those numbers trustworthy.

`restore_snapshot.sh` remains the authority. The template is a database at a fixed
schema, so `reset_db.sh` fingerprints the tree's migration files -- names and
contents -- and refuses to clone when they do not match what the template recorded.
It refuses rather than falling back to the slow path: a reset that is 1s most of the
time and 49s occasionally would put a 48-second spike inside a measurement loop at a
moment nobody chose.

## Built: the read screening matrix

`perf/scripts/screen_reads.py` enumerates every REST list endpoint from the URL resolver
at run time -- never from a list in a file, so it cannot rot as models come and
go -- and measures `list`, `list?depth=1`, `detail` and `detail?depth=1` for
each. 518 measurements in 84 seconds.

**Correcting the figure this section used to carry.** It claimed a surface of 330
list endpoints, 320 detail and 127 UI list views, and coverage of "roughly 5%".
The resolver reports **166 API list endpoints**, of which **6** are named in
`workload.yml`. Real coverage was **3.6%**, and the 330 was wrong.

The normalization is what makes it a screening instrument: cost per *returned
object*, not per request. A 5-row model and a 6,556-row one are not comparable
per request; per object they are, and an endpoint doing something per row shows
up regardless of its table size. Rank on pages of ten or more objects -- below
that, fixed overhead dominates and the ratio misleads.

What the first run found, against the tree with all sixteen accepted changes
applied, so these are residual costs:

| endpoint | q/object | duplicates | rows |
|---|---:|---:|---:|
| `dcim.cabletocabletermination?depth=1` | 19.5 | 473 | 6,556 |
| `dcim.interfaceconnections` | 14.4 | 345 | 2,780 |
| `vpn.vpntunnelendpoint?depth=1` | 9.8 | 226 | 37 |
| `circuits.circuit?depth=1` | 7.3 | 169 | 38 |
| `ipam.ipaddresstointerface?depth=1` | 5.9 | 130 | 2,937 |

Two of those are worse per object than anything the inner loop has measured.
`api.interface.depth1` at its original worst was 12.3 queries per object, and it
absorbed most of this branch's effort.

`dcim.interfaceconnections` is a clean N+1: page-size sensitivity gives an exact
**9 + 14n** fit -- 23 queries at limit 1, 79 at 5, 149 at 10, 359 at 25, 709 at
50 -- so 14 queries per row on a 2,780-row table. That makes it the largest
untouched N+1 the screen found.

An earlier version of this section read more into it than the data supports. It
said the endpoint showed "a different cost class" because its query count is
identical at depth 0 and depth 1. It is not: the endpoint simply **ignores
`depth`**. Diffing the two responses shows the only difference is the pagination
`next` link echoing `depth=1` -- exactly 8 bytes at every page size, which is
why the delta does not scale with object count. Identical counts across depth
therefore say nothing about cost class, and this is the ordinary per-row N+1
class the branch has been fixing all along. `powerconnections` and
`consoleconnections` share the shape at 5.4 q/obj and presumably the
explanation.

**A screening pass, not a regression gate.** 518 measurements do not belong in
the inner loop, which stays at 38 scenarios. Run this occasionally; its output is
a ranked list of where to point the next investigation. It also weights every
endpoint equally, which is a selection-free sample rather than a usage-weighted
one -- whether anyone lists `cabletocabletermination` at `?depth=1` in practice
is a product question, not a measurement one.

## Built: the write screening matrix

`perf/scripts/screen_writes.py` is the read screen's counterpart, built to the same three
rules. It enumerates from the URL resolver at run time; it takes writability from
the DRF router's own action map on the URL callback rather than inferring it from
the viewset class; and it normalizes to cost per *created* object. 152 of the 166
API list endpoints accept POST. 105 were measured across `create.x1`,
`create.x10` and `update.x1` — 257 measurements in 211 seconds. See finding 38.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/screen_writes.py \
        --out /source/perf/results/screen-writes.json
    # --dry-run   build payloads and report coverage; issue no requests
    # --isolation reset   commit for real, then reset_db.sh + one discarded request

**What it found.** The marginal cost of one more object — `(x10 - x1) / 9` — has a
median of **12 queries** across 98 models and a minimum of 3.0. The read screen's
median is 0.28 queries per returned object, and its worst endpoint anywhere is
19.5. The cheapest create on the whole write surface is more expensive per object
than 47 of the 49 read endpoints that return a full page.

| endpoint | marginal q/obj | x1 | x10 | duplicates |
|---|---:|---:|---:|---:|
| `ipam.ipaddresstointerface` | 65.6 | 77 | 667 | 627 |
| `dcim.interfaceredundancygroupassociation` | 46.2 | 55 | 471 | 430 |
| `dcim.cable` | 44.9 | 54 | 458 | 381 |
| `dcim.device` | 40.0 | 49 | 409 | 364 |
| `ipam.prefix` | 30.0 | 38 | 308 | 254 |
| `dcim.interface` | 23.8 | 35 | 249 | 219 |

`create.x1` alone would rank almost nothing: 9 queries at its cheapest and 20 at
the median, most of it fixed overhead. The bulk arm is what separates fixed cost
from per-row work, and Nautobot's API takes a JSON list on a list endpoint, so it
costs one extra request per model to get it.

**Coverage, which is the part that had to be built rather than measured.** 152
attempted / 130 payloads built / 105 accepted / 105 measured — and of the 105,
**82 came from field metadata alone and 23 needed an entry in a hand-maintained
exception list**. Both halves are printed, every run. A write screen can
under-report in a way the read screen cannot: a model whose payload is rejected
drops out and reads as *not a problem* rather than *not measured*, which is the
failure that left "330 endpoints, roughly 5%" in this file for weeks. So every
model lands in exactly one bucket with the reason it stopped there. The 47 that
did not make it are 21 rejected by model validation no generated payload can
satisfy, 19 whose required related model has no rows in this dataset, 4 whose
uniqueness constraint spans foreign keys over tables of fewer than ten rows, 2
returning HTTP 500, and 1 with no derivable value.

**Where the payloads come from.** `perf/scripts/payloads.py`, from `serializer.fields` —
the same source DRF's OPTIONS metadata is built from, read directly rather than
over HTTP so the field *objects* are available. That matters: a related field's
`queryset` and the model field's `limit_choices_to` are what make it possible to
pick a value that will validate. Three things were not obvious and all three are
now the difference between 70% coverage and 40%:

- `ForeignKeyLimitedByContentTypes.get_limit_choices_to()` returns a **dict**, and
  `queryset.filter(dict)` is a `FieldError`. Getting this wrong took out every
  model with a status or a role — 22 of 152, including device, interface, prefix,
  ipaddress, cable, location, circuit and rack.
- DRF's `required` is not the model's `blank`. A `blank=False` field with no
  default is still `required=False` on the serializer whenever the column is
  nullable, and `full_clean` then rejects what the payload omitted.
- A many-related field's child is not always a plain relation. `content_types`
  wants `"app_label.model"` strings, not primary keys.

**Isolation is rollback, and it was measured against the alternative.** Every
measured request runs inside `transaction.atomic()` that is rolled back — possible
at all only because the requests go through the Django test client and stay
in-process, where finding 29's "REST writes cross the process boundary" does not
apply. Row counts on seven tables are byte-identical before and after a full run
of 257 writes.

`--isolation reset` is the control, and it was run: both modes over the five
`dcim.device*` models, three rounds each way with the arms alternated. **Query
counts differ by exactly −2 on all 13 operations in every round** — the
SAVEPOINT/RELEASE pair, the same difference finding 29 measured on bulk create.
Wall clock is **+3.0% median for the committed arm**, which is an upper bound
rather than an estimate, because that arm carries the residual cold-buffer cost of
the clone before it.

Reset mode needed three corrections before it was a control rather than a trap,
and each is a way a write screen can produce confident wrong numbers:

- It cannot shell out to `reset_db.sh` — this runs inside the container and that
  script drives `docker compose` from the host. The clone goes over a second
  psycopg2 connection instead, carrying a reimplementation of the migration
  fingerprint. `--verify-reset` checks it against the shell one; they agree byte
  for byte.
- `force_login` writes a `django_session` row, and the clone replaces the database
  that row is in. Without re-logging-in after every reset, every subsequent
  request is anonymous.
- The post-clone throwaway request must be **rolled back even in reset mode**. A
  committed throwaway collides with the measured request on every unique name,
  which reads as a rejected payload rather than as a broken protocol.

**A rolled-back transaction restores the database, not the process.** The first
full run had 47 of 252 measurements with *unstable query counts*, nearly all
updates, because the natural-key and tag caches are cold only on the first pass
and survive the rollback that resets everything else. One discarded warmup run per
operation takes that to 1 of 257. Same discipline as finding 33's post-clone
warmup, arriving from the opposite direction — there the cold thing was
PostgreSQL's shared buffers, here it is the Python process.

**Two limits, stated rather than discovered later.** The ranking is queries per
object, so it is blind to a small number of expensive queries exactly as the read
screen was; `db_ms` is recorded per measurement, so that second ranking costs
nothing to add. And the payloads are minimal — required fields only — so every
figure is the *floor* cost of a create. Tags, custom field data and relationships
are omitted and they are write work.

## Built: whole-workflow A/B on two trees

`perf/scripts/arm_control.sh` and `perf/scripts/apply_arm.sh` run the same workload against two
different versions of Nautobot on one box. Built to settle whether the −37%
write-path win was real (finding 39); reusable for any claim that only shows up
at whole-workflow scale.

    perf/scripts/apply_arm.sh <git-ref> <label>    # empty db -> swap tree -> timed apply

Four things it does that a hand-run A/B would not, each because of a rule this
branch paid for:

- **Proves the arms differ before measuring.** It hashes `nautobot/**/*.py` and
  prints the digest per run, rather than trusting that a checkout happened. A
  `git stash` A/B once silently measured identical code six times.
- **Swaps `nautobot/` only, and unstages immediately.** `perf/` and
  `development/docker-compose.perf.yml` do not exist on `next`, so a whole-tree
  checkout deletes the harness and the compose overlay the running container was
  created with. All 37 files that differ are modifications — no adds, no deletes
  — so a path-scoped checkout is an exact swap both ways. It runs `git reset`
  straight after, because a staged reversion left lying around is how five fixes
  were undone once.
- **Restarts the container and then waits for the load to fall.** uwsgi has no
  autoreloader, so without a restart the next request runs the previous arm's
  code. And per finding 35, three workers importing Nautobot on two pinned cores
  bias a whole arm bimodally, which alternating rounds does not cancel.
- **Counts queries server-side.** `pg_stat_statements` is reset before the run and
  summed after, so each arm carries a deterministic counter next to its wall
  clock. One run settles the query comparison; only the wall-clock ratio needs
  alternating rounds.

**What it found, beyond the headline.** Of the 662 seconds the branch saves on a
datacenter apply, **2.9 are database execution time**. On the read side db time
and query count track each other closely; on the write side they come apart
completely, because the wins are serializer and natural-key work rather than SQL.
A write experiment that reports query count alone will undervalue exactly the kind
of fix this branch is best at.

## Built: a capture proxy, for when the evidence is in flight

`perf/scripts/capture_proxy.py` forwards every request to Nautobot unchanged and writes
the request and response bodies to disk on any 5xx.

    python perf/scripts/capture_proxy.py --listen 8199 --target http://localhost:8180
    NAUTOBOT_URL=http://localhost:8199 databot apply ...

It exists because Django logs `Internal Server Error: /path` and Nautobot's
exception middleware renders the traceback into a short JSON body, so for a
failure that only happens under a real client, neither the container log nor the
client's own warning says what broke. It is not a measurement tool — it adds a
hop and serializes requests, so nothing timed should run through it.

It earned itself immediately: it recorded **zero 5xx from Nautobot** across a
cable phase that databot reported as 16 failed bulk creates, which is what
redirected finding 40 from "Nautobot throws on 100 cables" to "the client stops
listening at 30 seconds".

## Environment quick reference

| | |
|---|---|
| Nautobot UI | http://localhost:8180 (admin / admin) |
| API token | `0123456789abcdef0123456789abcdef01234567` |
| celery_worker | 8181; Postgres and Redis are not published to the host |
| Compose project | `nautobot-perf-3-3` (all commands via `perf/scripts/dc.sh`) |
| Dataset | databot `enterprise-campus / large / seed 42`, 24,091 objects |
| Reset (fast) | `perf/scripts/reset_db.sh` — 1.3s template clone; discard one request after |
| Restore (authority) | `perf/scripts/restore_snapshot.sh` (uses `perf/snapshot-large.sql`, gitignored) — 49s |

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

**Measurement note.** `perf/scripts/tier2_latency.py` runs against any URL with a token, so it works
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
