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

## PR 3 — API | Targeted

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 9 | `036404a90` | Targeted \| 1 — Device parent_bay reverse one-to-one | `open` | Triaged as probably ours (cf finding 25) — unverified. |
| 10 | `26222c5fd` | Targeted \| 2 — FrontPort/RearPort cable_peer prefetch | `open` | Triaged as probably ours (cf findings 26, 34, 36) — unverified. |
| 11 | `c4dfe22a8` | Targeted \| 3 — Job task_queues; UserSavedViewAssociation chain | `open` | Probably new. |

## PR 4 — API | Behavioral

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 12 | `68920cacc` | Behavioral \| 1 — memoize serializer URL route shapes | `open` | Probably new. Finding 51 memoized reversals in the nav menu only, so the sites are disjoint. |
| 13 | `26e6dd995` | Behavioral \| 2 — get_absolute_url and form data-url shapes | `open` | Probably new. |
| 14 | `a714d9267` | Behavioral \| 3 — NATURAL_SLUG_ENABLED opt-out | `open` | A settings-only behaviour change, not a perf fix; assess as a product decision. |

## PR 5 — UI | Views

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 15 | `5082f4a10` | Views \| 1 — Device LLDP neighbors prefetch | `open` | Probably new. |
| 16 | `bb09ebf60` | Views \| 2 — changelog views prefetch changed_object | `open` | Overlaps finding 56 (home page panel) and finding 63 (REST optimizer); both are different code paths, so this may still stand. |

## PR 6 — UI | BaseTable  — **the collision block; do it as one unit**

Eight commits in `nautobot/core/tables.py`, which already carries findings 7, 11 and 54. The
queue's standing instruction: do not interleave this block with anything else.

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 17 | `c3fb5aee0` | BaseTable \| 1 — accessor walk keeps to-many segments | `open` | |
| 18 | `34e8d3c61` | BaseTable \| 2 — explicit `columns=` kwarg | `open` | Enabling change for their column audit. |
| 19 | `bb3255f01` | BaseTable \| 3 — ContentTypesColumn keeps its prefetch cache | `open` | |
| 20 | `054c7009b` | BaseTable \| 4 — prefetch relationship associations | `open` | |
| 21 | `41e69ca83` | BaseTable \| 5 — RelationshipColumn compares by ID | `open` | |
| 22 | `5c40c77d5` | BaseTable \| 6 — `display_prefetch_related` convention | `open` | The convention PR 7 and PR 8 are built on; assess it before any of them. |
| 23 | `06276a82e` | BaseTable \| 7 — LinkedCountColumn honors it | `open` | Depends on 22. |
| 24 | `867cb152e` | BaseTable \| 8 — `replace_queryset()` helper | `open` | Consumed by PRs 7 and 8. |

## PR 7 — UI | Tables

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 25 | `f047b8d47` | Tables \| 1 — batch Prefix hierarchy lookups | `open` | Lands on an open queue item. Their figure: Prefixes tab 264→60. |
| 26 | `9c6431d23` | Tables \| 2 — Device tables prefetch primary_ip | `open` | Overlaps finding 53; verify before spending a round. |
| 27 | `ecdfdf4ea` | Tables \| 3 — Location tree-link via tree_depth | `open` | Lands on an open queue item. Their figure: LocationType detail 155→59. |
| 28 | `4eab9c8d0` | Tables \| 4 — display_prefetch_related declarations | `open` | Depends on 22. |
| 29 | `c5d3cd136` | Tables \| 5 — interface IP columns prefetch namespace | `open` | |
| 30 | `ba5cbadaa` | Tables \| 6 — extras tables join per-row FK/GFK reads | `open` | |
| 31 | `fd0aa7b4e` | Tables \| 7 — parent objects read by name links | `open` | |
| 32 | `b28f73e43` | Tables \| 8 — action buttons stop querying per row | `open` | `ButtonsColumn` is 0.765ms/cell in finding 50's attribution. |
| 33 | `cafdbbe7f` | Tables \| 9 — JobTable batch-prefetches latest results | `open` | |
| 34 | `f2d45ff6b` | Tables \| 10 — utilization columns prefetch inputs | `open` | `PowerFeed.utilization` is a named row-scaling item in the queue. |

## PR 8 — Cabling

| # | Hash | Commit | Status | Verdict |
|---|---|---|---|---|
| 35 | `61e2fffef` | Cabling \| 1 — nested-serialization cable-peer prefetches | `open` | Triaged as probably ours (cf findings 2, 9, 26) — unverified. |
| 36 | `7c3a785ee` | Cabling \| 2 — CableTerminationTable self-applies | `open` | Their audit: 247→19 offenders in the port tables. |
| 37 | `375835e6b` | Cabling \| 3 — connection columns prefetch far-end devices | `open` | |
| 38 | `bbe6061ee` | Cabling \| 4 — CableTable self-applies | `open` | |

## Tally

| status | count |
|---|---|
| `ported` | 3 (findings 62, 63, 64) |
| `superseded` | 3 |
| `declined` | 2 |
| `open` | 30 |
