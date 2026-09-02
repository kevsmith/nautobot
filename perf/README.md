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

## Scope rules

**In scope:** Python/Django application code — query construction,
`select_related`/`prefetch_related`, serializers, table/column rendering,
caching, pagination, avoiding per-row work in loops.

**Out of scope, log don't fix:** anything requiring a migration. No new indexes,
no schema changes, no denormalization, no Postgres tuning. These go in the
report's **non-actionable findings** list with evidence and expected impact, for
review later.

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
