# Import ledger: the perf series on Ken's fork

**One row per commit, 38 of them.** The queue's "Imported candidates" section argues the
ordering and the two structural hazards; this file is the bookkeeping — what has been
looked at, what it turned into, and what nobody has opened yet. Update the row **in the same
commit** that records the finding, so the ledger cannot drift from the findings directory.

Source: `origin/vibed-api-ui-improvements` in `~/repos/experiments/perf/ken-nautobot`,
based on `25e55bb37` (develop, 2026-07-24), indexed by `pr-breakout-manifest.md` at
`43ae11c27`. None of it is upstream as of 2026-09-10.

**The numbering is our assessment order, nothing more.** It follows his manifest so the two
documents can be read side by side; it is not a recommendation about the order upstream should
take these, and nothing here assumes his PR boundaries survive review.

## Status vocabulary

| status | meaning |
|---|---|
| `open` | not yet read against our tree |
| `assessed` | read and attributed; verdict recorded, no experiment needed |
| `ported` | re-implemented here and recorded as a finding |
| `declined` | assessed or measured, and deliberately not taken — reason in the row |
| `superseded` | our tree already achieves it, by our own mechanism |

A row is only `superseded` when something on this branch was *measured* to cover it, not
when the code merely looks equivalent.

## PR 1 — API | Caching  (closed 2026-09-11, 6/6)

Assessed together, because the whole PR aims at one shape: a value read once per serialized
object where each read is a Redis round trip. The attribution instrument is
`perf/scripts/probe_cache_gets.py`, which counts cache-backend GETs per key at 25 and 100
rows over 16 endpoints — a key that does not grow with the page is already memoized.
**Fifteen of sixteen endpoints have no key that scales with row count**, so findings 4, 5 and
6 had already closed this shape everywhere but one site.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 1 | `6d2582b93` | Caching \| 1 — natural_key_field_lookups per class | `declined` | Findings 4+5 memoize the same recipe request-scoped. His is process-wide and invalidated **in-process only**, so a sibling uwsgi worker keeps a stale recipe after a Location-tree deepening until it restarts. Our scope covers REST reads, `serialize_object_v2` and `web_request_context` but not UI views — and the probe shows the UI residual is **Python recomputation, not Redis**, because the Constance read inside it is already request-memoized by finding 6. Same win, worse staleness. |
| 2 | `08af89a00` | Caching \| 2 — natural_slug once per object | `superseded` | Finding 1, same site, same fix. Ours falls back on `AttributeError` from the property; his returns `"unknown"` for that case. |
| 3 | `089ee666a` | Caching \| 3 — Location lookups override | `superseded` | Finding 4's decorator is already on that override. |
| 4 | `e81c233b8` | Caching \| 4 — custom_field_keys per field instance | `superseded` | `keys_for_model` goes through `cache_get_or_set`, which memoizes into `request_cache()` — one GET per model per request, and it covers every such site rather than this one field. Measured: `api.interface.depth1` reads it once per *distinct model*, flat in row count. |
| 5 | `56f4e299f` | Caching \| 5 — signal-invalidated config memo | `declined` | Finding 6 memoizes `get_settings_or_config` per request for all keys. Residual is one Redis read per key per request; his removes that at the cost of cross-worker staleness on config. Not a trade worth taking for microseconds. |
| 6 | `fd584db6a` | Caching \| 6 — ProcessTTLCache at three sites | `ported` (in part) | `keys_for_model`: superseded, see row 4. `get_celery_queues`: not request-memoized here, but not per-object in any measured read — left alone. `TreeModel.display`: **real**, 1.000 GET/row on `api.location.depth1`. Taken as **finding 64** (`cd176d9bb`), by our mechanism (`cache_get_or_set`, request-scoped) rather than his 5-second process TTL: 97 -> 5 GETs, −4.4% wall, control flat. |

## PR 2 — API | Generic  (done 2026-09-10/11, 2/2)

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 7 | `2faf6adad` | Generic \| 1 — prefetch depth-0 FKs instead of JOINing | `ported` | **Finding 62.** Gated on wall clock and row width, not query count — the deterministic counter cannot see it. |
| 8 | `d14c9fe57` | Generic \| 2 — auto-prefetch GenericForeignKeys | `ported` | **Finding 63.** 8 endpoints improved, 508 unchanged, 0 worse. |

## PR 3 — API | Targeted  (closed 2026-09-11, 3/3)

Attributed with `probe_query_slope.py` (queries per rendered row) and, for the two zero-row
endpoints, `probe_ports_nplus1.py` (rows synthesised inside a rolled-back transaction).
**Two of the three sites this PR fixes are on endpoints with no rows on either snapshot**, which is
why the triage in `queue.md` could not tell whether they were already ours.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 9 | `036404a90` | Targeted \| 1 — Device parent_bay reverse one-to-one | `superseded` | **By upstream, not by us** — `eed5db211` (#9326) is already in our base `next`, and `DeviceViewSet` carries `parent_bay` there. The queue's triage credited finding 25; that finding is the *nested* case, `InterfaceViewSet.select_related("device__parent_bay")`. Both exist; nothing to port. |
| 10 | `26222c5fd` | Targeted \| 2 — FrontPort/RearPort cable_peer prefetch | `ported` | **Not ours after all.** Seven of nine termination viewsets carry the prefetch; these two carry none at all, since neither is a `PathEndpoint`. **Finding 65** (`dcd487473`): 1.000 -> 0.000 q/row at depth 0 on synthesised rows. Zero rows on both snapshots, so no wall clock exists. Depth 1 still scales — left to `Cabling \| 1`. |
| 11 | `c4dfe22a8` | Targeted \| 3 — Job task_queues; UserSavedViewAssociation chain | `ported` (Job half) | Job: **finding 66** (`ccd08a3f0`), 1.000 -> 0.000 q/row, −19.4% wall, control flat. UserSavedViewAssociation: **blocked on the dataset**, zero rows on both snapshots; see the open-items note below. |

## PR 4 — API | Behavioral  (closed 2026-09-11, 3/3)

Attributed with `probe_url_reversals.py`, after fixing the hole that made it report zero reversals
for the REST API entirely (it patched `django.urls.reverse` only; `rest_framework.reverse` had
bound the function at import time). A 100-row interfaces page reversed **1,031 routes**, 9.9 per
row; the two memos take it to **6**.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 12 | `68920cacc` | Behavioral \| 1 — memoize serializer URL route shapes | `ported` | **Finding 67** (`ff93cf3e1`): 1,031 -> 542 reversals, −20.6% on `devices?depth=1`, −6.8% on interfaces, control flat. Disjoint from finding 51 as triaged. |
| 13 | `26e6dd995` | Behavioral \| 2 — get_absolute_url and form data-url shapes | `ported` | **Finding 68** (`90a00176e`): 542 -> 6 reversals, −5.5% on interfaces (−11.9% for the pair). Two departures from theirs: the script prefix is in both memo keys, and `get_absolute_url` keeps its original walk as a fallback instead of raising. |
| 14 | `a714d9267` | Behavioral \| 3 — NATURAL_SLUG_ENABLED opt-out | `declined` | **Not a perf change; an API contract change with a perf payoff for whoever turns it off.** With the flag off, `natural_slug` serializes as `""` while staying in the schema, and the natural-key prefetches are skipped. Default is on, so it measures zero by default, and its value is a deployment choice rather than a branch result. It also interacts with findings 1, 4, 5, 14 and 42, which all optimize the path it bypasses. **Kevin's call, and an upstream conversation rather than a perf experiment** — declined here so the branch does not carry a feature flag nobody asked for. |

## PR 5 — UI | Views  (closed 2026-09-11, 2/2)

Neither page is in `perf/workload.yml`, so neither had ever been measured here — the read loop does
not name them and the read screen is REST-only.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 15 | `5082f4a10` | Views \| 1 — Device LLDP neighbors prefetch | `ported` | **Finding 69** (`fa9347675`): **875 -> 34 queries**, 17.857 -> 0.694 per interface, **−81.3% wall** (1,320.7 -> 246.4 ms). The largest single-page reduction on the branch, on a page no instrument here had ever touched. |
| 16 | `bb09ebf60` | Views \| 2 — changelog views prefetch changed_object | `superseded` | **Measured, not assumed.** `ObjectChangeTable` declares `add_conditional_prefetch("object_repr", "changed_object")`, which applies whenever that column is visible and the data is a queryset — however the table was built, including both views this commit patches. The dataset could not show it (36,552 ObjectChange rows, but the busiest single object has **two**), so `probe_changelog_gfk.py` synthesises 100 rows for one device in a rolled-back transaction: the tab reads **31 queries at 25 rendered rows and 31 at 100**, and the global list 10 at both. Flat, so there is nothing left to prefetch. |

## PR 6 — UI | BaseTable  (closed 2026-09-12, 8/8) — the collision block, done as one unit

Eight commits in `nautobot/core/tables.py`, which already carries findings 7, 11 and 54. The
queue's standing instruction: do not interleave this block with anything else.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 17 | `c3fb5aee0` | BaseTable \| 1 — accessor walk keeps to-many segments | `ported` | **Finding 71** (`7b5ca339b`). A static scan of all 183 table classes finds **exactly one** affected column, and it is hidden by default: 1.000 -> 0.000 q/row measured with it made visible. No page moves. |
| 18 | `34e8d3c61` | BaseTable \| 2 — explicit `columns=` kwarg | `ported` | **Finding 72** (`67d9634e3`). Enabling change, removes nothing — and it is what made findings 71 and 73 measurable at all, since optimization keys off *visible* columns. |
| 19 | `bb3255f01` | BaseTable \| 3 — ContentTypesColumn keeps its prefetch cache | `ported` | **Finding 70** (`5ee52a8af`): 1.000 -> 0.000 q/row; `/extras/roles/` 54 -> 9 queries and **−30.5% wall**, `/extras/statuses/` 31 -> 9 and −27.5%. |
| 20 | `054c7009b` | BaseTable \| 4 — prefetch relationship associations | `ported` | **Finding 73** (`e009e62d9`), with commit 21 — neither is measurable alone. |
| 21 | `41e69ca83` | BaseTable \| 5 — RelationshipColumn compares by ID | `ported` | **Finding 73** (`e009e62d9`): **4.200 -> 0.000 q/row**, the largest per-row cost in the block, and invisible by default. |
| 22 | `5c40c77d5` | BaseTable \| 6 — `display_prefetch_related` convention | `ported` | **Finding 74** (`750669f74`), with commit 23. `DeviceTable.device_type` 1.000 -> 0.000 q/row in isolation; `/dcim/devices/` does not move, because the list viewset already joins that path. |
| 23 | `06276a82e` | BaseTable \| 7 — LinkedCountColumn honors it | `ported` | **Finding 74** (`750669f74`). |
| 24 | `867cb152e` | BaseTable \| 8 — `replace_queryset()` helper | `ported` | **Finding 75** (`ae4a7a88c`). Enabling change; taken now so the later blocks need no change to `core/tables.py`. |

## PR 7 — UI | Tables  (closed 2026-09-12, 10/10)

Taken as two commits rather than ten, because on this tree they are two mechanisms: batching the
hierarchy lookups (1 and 3) and declaring what a column reads when the accessor walk cannot see it
(the other eight). Attributed by `probe_table_column_audit.py`, which prices every column of every
table on its own — the audit their series ran, reproduced here rather than taken on trust.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 25 | `f047b8d47` | Tables \| 1 — batch Prefix hierarchy lookups | `ported` | **Finding 76** (`0d30bbdc0`): 3.000 -> 0.000 q/row, `/ipam/prefixes/` 311 -> 17 queries and **−41.3% wall**. Worst per-row page of the 21 screened. |
| 26 | `9c6431d23` | Tables \| 2 — Device tables prefetch primary_ip | `ported` | **Finding 77** (`2ab871c2f`): 1.000 -> 0.000 q/row. Overlaps finding 53 (the REST side); this is the table side. |
| 27 | `ecdfdf4ea` | Tables \| 3 — Location tree-link via tree_depth | `ported` | **Finding 76** (`0d30bbdc0`): 1.973 -> 0.000 q/row, `/dcim/locations/` 206 -> 10 queries and **−32.9% wall**. |
| 28 | `4eab9c8d0` | Tables \| 4 — display_prefetch_related declarations | `ported` | **Finding 77**. `/circuits/circuits/` 46 -> 9 queries, **−22.1% wall**. Four of the six declarations are unmeasured here for want of rows. |
| 29 | `c5d3cd136` | Tables \| 5 — interface IP columns prefetch namespace | `ported` | **Finding 77**: `InterfaceTable.ip_addresses` 3.250 -> 1.750 q/row; the residual is the row-attr floor, not this column. |
| 30 | `ba5cbadaa` | Tables \| 6 — extras tables join per-row FK/GFK reads | `ported` | **Finding 77**: `AssociatedContactsTable` 1.000 -> 0.000 q/row on four columns; the other five tables it touches hold too few rows here to show a slope. |
| 31 | `fd0aa7b4e` | Tables \| 7 — parent objects read by name links | `ported` | **Finding 77**: `DeviceTable.parent_device` 1.000 -> 0.000 q/row. |
| 32 | `b28f73e43` | Tables \| 8 — action buttons stop querying per row | `ported` | **Finding 77**. The rack-elevation button now uses `location_id`; `VLANGroup.available_vids()` iterates `.all()` so a prefetch is honored. |
| 33 | `cafdbbe7f` | Tables \| 9 — JobTable batch-prefetches latest results | `ported` | **Finding 77**. Also removes a `.only("status")` that was itself the defect: the Last Run column then loaded two deferred fields and a user per row. |
| 34 | `f2d45ff6b` | Tables \| 10 — utilization columns prefetch inputs | `ported` | **Finding 77**. `Rack.get_utilization()` now filters a prefetched `devices` cache in Python instead of re-filtering the queryset per row. |

## PR 8 — Cabling  (closed 2026-09-12, 4/4)

Three ported as one commit, one rejected on measurement. The audit that drove PR 7 is what found the
target here: every column of the two interface tables sat at the same 1.750 q/row floor, which is a
row-level cost (`row_attrs` -> `cable_status_color_css` -> `record.cable`) that no per-column fix can
reach.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 35 | `61e2fffef` | Cabling \| 1 — nested-serialization cable-peer prefetches | `declined` | **Implemented, measured, reverted — finding 79.** `/api/dcim/interfaces/?depth=1` is **0.053 q/row here before the change**, already better than the 0.34 their report quotes after it; the enrichment takes it to 36/41 queries (slope 0.067) — ten added, none removed. Redundant against findings 2, 9, 26, 34, 36, 62 and 63 rather than wrong. |
| 36 | `7c3a785ee` | Cabling \| 2 — CableTerminationTable self-applies | `ported` | **Finding 78** (`977eb44c6`). The 1.750 q/row floor on **every** interface-table column: `mtu` 41 -> 1 query at 20 rows. This is what PR 7 could not reach. |
| 37 | `375835e6b` | Cabling \| 3 — connection columns prefetch far-end devices | `ported` | **Finding 78**: `ConsoleConnectionTable.console_server` 8.000 -> 321/3 queries, `console_server_port` 5.000 -> 201/3, `PowerConnectionTable.pdu` 8.000 -> 321/4. |
| 38 | `bbe6061ee` | Cabling \| 4 — CableTable self-applies | `ported` | **Finding 78**: `CableTable.termination_a_parent` 8.000 q/row -> 321/2 queries at 40 rows, flat. |

## Blocked on the dataset, not on judgement

Two sites in this series sit on endpoints with zero rows on both snapshots, so nothing here can
price them:

- `UserSavedViewAssociation.select_related("saved_view__owner", "user")`, the other half of
  `Targeted | 3`. `savedview` and `usersavedviewassociation` are both empty.
- The depth-1 residual finding 65 left behind, which `Cabling | 1` targets — measurable only on
  synthesised ports.

`probe_ports_nplus1.py` shows the way through: synthesise rows to the model's own shape inside a
transaction that is always rolled back, and gate on the slope. It is a weaker claim than a measured
endpoint and a much stronger one than reading the diff, and it is available for any of the 66
zero-row endpoints in `perf/dataset-gaps.md`.

## Tally

| status | count |
|---|---|
| `ported` | 27 (findings 62-78) |
| `superseded` | 5 |
| `declined` | 4 (one of them measured and reverted: finding 79) |
| `open` | **0 — the series is fully assessed** |
