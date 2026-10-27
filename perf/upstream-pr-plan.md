# Upstream PR plan for perf/recommended

Drafted 2026-10-20 from `perf/recommended` at `f6a4363d0`, checked against `upstream/develop` at
`72ad4e75c` (3.2.8a0) and `upstream/next` at `4c38263d4` (3.3.0a0). On 2026-10-27 `perf/recommended` was rebased onto `upstream/next`
at `33ea108ce` and every SHA below that names one of its commits was remapped to the rebased commit.

## Ground rules from upstream

- Fixes and minor features target `develop`; larger features target `next`
  (`nautobot/docs/development/core/getting-started.md:8-11`). `develop` merges forward into `next`.
- **PRs are reviewed only for accepted issues** (same file, line 839). Each PR below needs an issue
  first, or one umbrella issue the maintainers agree to accept PRs against. Ask which they prefer.
- Branch names follow `u/<github-user>-<issue>-<summary>`, and every PR needs a `changes/<issue>.<type>`
  fragment for towncrier. No commit on `perf/recommended` has one.
- 39 of the 55 commits carry no test. Each PR should add a query-count test (`assertNumQueries` or
  the per-row counters the findings used) that fails on the base branch, so a reviewer can verify the
  claim without the `perf/` harness.

## Branches

On `origin` (`kevsmith/nautobot`). 1b, 3a, 3b and PR 4 (then `u/kevsmith-home-page-changelog-panel`) were rebased onto
`upstream/develop` at `14d0be00d` on 2026-10-27, with no conflicts and none of develop's new commits touching their
files; the closed and merged branches stay on `72ad4e75c` (1, 2, 3).
A branch without an issue carries `changes/XXXX.fixed.md`, to be renamed to the issue number, and its
name gains the issue number once there is one.

| PR | Branch | Head | Commits | Verified on 3.2 |
|---|---|---|---|---|
| 1 | `u/kevsmith-9668-device-list-primary-ips`, issue [#9668](https://github.com/nautobot/nautobot/issues/9668), PR [#9679](https://github.com/nautobot/nautobot/pull/9679), **closed 2026-10-22 as a duplicate of community PR [#9672](https://github.com/nautobot/nautobot/pull/9672)** | `a45e15327` | F53. F69 moved to PR 1b. | yes: test fails without, passes with; `dcim.tests.test_views` 2,486 OK |
| 2 | `u/kevsmith-9680-table-column-per-row-queries`, issue [#9680](https://github.com/nautobot/nautobot/issues/9680), PR [#9681](https://github.com/nautobot/nautobot/pull/9681), **merged 2026-10-23 as `c418be992`** | `34bee48aa` | F70 `9a2699805`, F71 `9004d443a`, F73 `38d2b0e7b`, test cleanup `ad815c5c2`, accessor-walk test `34bee48aa` | yes: all 3 tests fail without, pass with; `core.tests.test_tables` 21 OK |
| 1b | `u/kevsmith-9692-lldp-neighbors-connected-endpoints`, issue [#9692](https://github.com/nautobot/nautobot/issues/9692), PR [#9693](https://github.com/nautobot/nautobot/pull/9693) | `421e76dac` | F69 `c782a4bc2`, squashed with its test; review fix `f127d0d89`; two review suggestions applied on GitHub | yes, on a 3.2 image rebuilt 2026-10-26: test fails on develop (79, 113, 147 queries over 3 steps) and on `79e15adc8` (54, 56, 58), passes with; `dcim.tests.test_views` 2,486 OK; 1,123 -> 47 queries at 49 cabled interfaces |
| 3 | `u/kevsmith-9682-rest-viewset-prefetches`, issue [#9682](https://github.com/nautobot/nautobot/issues/9682), PR [#9683](https://github.com/nautobot/nautobot/pull/9683), **closed 2026-10-23 and split into 3a and 3b** | `7d500e2ca` | F25 `0a661d9bd`, F65 `bdf2337cd`, F66 `7d500e2ca` | yes: all 4 tests fail without, pass with; `dcim.tests.test_api` 1,692 OK, `extras.tests.test_api` 1,479 OK |
| 3a | `u/kevsmith-9682-cable-peer-and-job-queue-prefetches`, issue [#9682](https://github.com/nautobot/nautobot/issues/9682), PR [#9711](https://github.com/nautobot/nautobot/pull/9711), **merged 2026-10-27 as `50891a13a`** | `be3f48eea` | F65 `6d246f8bb`, F66 `c21f5c269`; review comment wording `be3f48eea`, applied on GitHub | yes, on `7a00ceaaa`: 12 `test_api` modules and `extras.tests` 5,043 OK once docs are built (27 `test_get_docs_url` fail under `--skip-docs-build` only); `core.tests.test_graphql` 101 OK |
| 3b | `u/kevsmith-9682-nested-reverse-one-to-one`, issue [#9682](https://github.com/nautobot/nautobot/issues/9682), PR [#9712](https://github.com/nautobot/nautobot/pull/9712), **merged 2026-10-27 as `fe9080eca`** | `796e4befb` | replaces F25 with an optimizer change, `efea5e343`; batched review suggestions `4b96caf48`; review fixes `8bcb67b10`, `796e4befb` | yes, on `7a00ceaaa`: both tests fail without (52 and 3 per-row `dcim_devicebay` queries), pass with; all 13 `test_api` modules, 5,320 OK. Review fixes on `14d0be00d`, 2026-10-27: the device test fails at both depths without the `OneToOneRel` join (6 per-row queries each), passes with; all 13 `test_api` modules, 5,320 OK at `2b601ba67`, `DeviceTest` 43 OK at `796e4befb` |
| 4 | `u/kevsmith-9725-home-page-changelog-panel`, issue [#9725](https://github.com/nautobot/nautobot/issues/9725), PR [#9726](https://github.com/nautobot/nautobot/pull/9726) | `90bdd8b65` | F56, ported to develop with its test, `90bdd8b65` (`5c011100c` with the fragment renamed) | yes, on `14d0be00d`, 2026-10-27: the test fails without (10 `dcim_location` queries for 12 rows), passes with; `core.tests.test_views` 93 OK, `extras.tests.test_homepage` OK. Home page 20, 22, 30, 40 queries at 5, 6, 10, 15 panel rows before; 11, 12, 12, 12 after |
| 9 | `u/kevsmith-cable-create-path-rebuilds`, issue and PR drafted 2026-10-27, not yet filed | `332622711` | F48, ported to develop (`50891a13a`) with a test, `332622711` | yes, on `50891a13a`, 2026-10-27: the test fails without (`rebuild_paths()` 2 calls, expected 1), passes with; `CableTest` 46 OK, `CableToCableTerminationTest` 32 OK, `test_cablepaths` 70 OK. Two-ended REST create 245 -> 226 queries, `rebuild_paths()` 2 -> 1; PATCH replacing both ends 180 -> 149, 3 -> 2. The remaining extra rebuild on PATCH is a separate bug: `Cable._orig_status` is always `None` for a cable loaded from the database, because `from_db()` sets `_state.adding` after `__init__()`, so every `Cable.save()` rebuilds paths |

Each commit pairs a fix with a test that should fail without it. Commits ported from Ken Celenza's fork
(F65, F66, F70, F71, F73) credit him with `Co-authored-by`. "Verified" means the new tests fail with the
fixes reverted and pass with them, and the touched test modules pass in full, on a 3.2 image built from
`develop` on albert, 2026-10-20.

PR 1's `a45e15327` applies review feedback from #9679: a test that matches SQL by table name builds the name
with `connection.ops.quote_name(Model._meta.db_table)`, since MySQL quotes with backticks and a hard-coded
`"` never matches there. PR 3's interface test had the same pattern and now quotes the same way. PR 3's `task_queues` docstring is back to
one line, with a one-line comment for the change.

PR 3 was closed after review on #9683 asked why only the interfaces viewset joined `device__parent_bay`, and
whether it could check the request depth. Every viewset that nests a Device at `?depth=1` has the same per-row
query, as F25's own note said. PR 3b fixes it in `ModelViewSetMixin.get_queryset()`: for a nested serializer it
also joins each reverse one-to-one that serializer includes. A nested serializer exists only at depth > 0, so the
join does too. PR 3a carries F65 and F66 unchanged. Both are partial fixes for #9682 and reference each other.
3a's changelog fragment is `changes/9682.fixed.md` and 3b's is `changes/9682.fixed.1.md`; towncrier 25.8.0 lists
all three lines under #9682. Both were verified on a 3.2 image rebuilt on albert, 2026-10-26, after upstream
bumped `opentelemetry-api` to 1.45.1.

PR 1b's `f127d0d89` answers review on #9693: the tab rendered a peer's virtual chassis name and a circuit's
provider, and the shared connection prefetch joined neither, so each still cost a query per row. `Interface` now
overrides `connection_destination_querysets()`, as `ConsolePort` and `PowerPort` do, and the tab uses
`connection_destination_prefetch()`. The test's peer is a virtual chassis member and each step adds a circuit;
removing either join fails it (49, 50, 51). The prefetch also skips the breakout data the template never reads,
so the tab runs 47 queries, 4 fewer than `79e15adc8`. It squashes an unpushed view-local `GenericPrefetch`
commit, `7f3c4e1ce`, from an earlier session.

PR 3b's `8bcb67b10` and `796e4befb` answer Glenn Matthews's review on #9712. The nested-serializer check is
narrowed to `ModelSerializer`, which every depth > 0 nested serializer is. He also asked whether the prefetch tuple
should include `OneToOneRel`. It already matches, as a `ManyToOneRel` subclass, but that branch sees only many-valued
fields. The gap was the single-valued branch, which joined only a `ForeignKey`, so a reverse one-to-one on the
viewset's own model was never joined; `DeviceViewSet` named `parent_bay` by hand. The check is now
`(ForeignKey, OneToOneRel)` and Device's hand-written join is gone. Upstream's
`DeviceTest.test_list_query_count_with_device_bays` did not guard it: `AssertNoRepeatedQueries` allows 10 repeats
and the test lists 6 devices, so it passed with no join at all. It now checks for the per-row `installed_device_id`
query directly, at depth 0 and depth 1. `2b601ba67` differs from `796e4befb` in that test only.

PR 2's `ad815c5c2` fixes a CI failure in `TableTestCase.test_base_table_apis`. `BaseTable.__init__` adds
relationship columns to the table class's `base_columns`, and a test's rollback does not invalidate the cached
relationships, so a Relationship created in a test leaks its columns into later tables in the same process. A
test that creates one deletes it and pops its columns on cleanup. By the same code, a deleted custom field or
relationship may keep its column in a running worker until restart; not verified, not reported upstream.

PR 2's `34bee48aa` answers review on #9681 asking what the accessor walk produces for each accessor shape.
A comment above the walk gives examples, and `BaseTableAccessorWalkTestCase` pins the `select_related` and
`prefetch_related` for nine shapes. Against develop's walk only the two FK-then-to-many cases fail, and they are
the two this PR changes. A reverse many-to-many hop (`ManyToManyRel`) is still not followed; that predates
the PR and is left out of scope.

A REST test that renders a related object at `?depth=1` has to grant view permission on that model too.
Without it Nautobot renders the nested object as a brief reference, the per-row query never happens, and
the test passes with or without the fix. PR 3's interfaces test did exactly that until it granted
`dcim.view_device`.

## How the dependencies were established

Every commit was cherry-picked alone onto both branches. Where it conflicted, earlier commits touching
the conflicting files were added until it applied, then removed one at a time to find the minimal set.
A second pass found commits that use a function or constant an earlier commit introduced, which a clean
cherry-pick does not reveal. Each PR below was then applied as a whole, prerequisites included, to
its target branch. All apply cleanly. None has had its tests run on the target branch yet.

Commits that apply only to `next` conflict with code `develop` does not have: `next` carries 190
commits `develop` lacks, seven of them in `core/views/utils.py` alone. They could be ported, at the
cost of rewriting them against 3.2 code.

## The PRs, in proposed order

`F` numbers are findings in `perf/findings/`. "Needs" lists PRs that must merge first.

### Wave 1: small, self-contained, on develop

These establish that the PRs are small, tested and reproducible before anything touches shared code.

| PR | Change | Findings and commits | Needs | Tests today | Evidence for the description |
|---|---|---|---|---|---|
| 1 | Device list primary IPs | F53 `731a34d4b` | - | has one | Closed as a duplicate: #9672 makes the same `select_related("primary_ip4", "primary_ip6")` change for #9668 |
| 1b | LLDP neighbors tab | F69 `c782a4bc2` | - | none | LLDP tab 875 queries on one page, 17.9 per interface (measured on `next`) |
| 2 | Per-row queries in shared table columns | F70 `0b72e2a50`, F71 `29e356d67`, F73 `07910a8d9` | - | none | 1.000 content-type query per row (ContentTypesColumn); 1.000 per row on to-many accessors; 4.200 per row on relationship columns |
| 3 | Missing prefetches on REST viewsets | F25 `703a3f50b`, F65 `ead2cfdf3`, F66 `6d854c14b` | - | none | parent_bay join −5.8% on interfaces at depth 1; 1.000 query per row on front/rear-port cable peers and on Job task queues, 0 after |
| 4 | Home page changelog panel | F56 `a6d1743fe` | - | has one, which conflicts on develop and needs porting | 25 of the home page's 79 queries are one panel rendering 15 rows |

### Wave 2: query fixes in shared code, on develop

| PR | Change | Findings and commits | Needs | Tests today | Evidence |
|---|---|---|---|---|---|
| 5 | Fetch each cable path once; prefetch connection endpoints | F2 `95261d599`, F30 `66b9eb4c8`, F34 `570c05f63`, F36 `a3f66427d` | - | none | interface-connections −61% at limit 50; power-connections −51%; cable terminations −76% |
| 6 | Table queryset seam and per-table batching | F75 `c13e966d4`, F76 `dbbb591de`, F77 `a1078cef0` | - | none | 3.000 queries per row on prefixes and 1.973 on locations, both to 0; 55 offending columns across 4 tables |
| 7 | Ancestor walks in blocks | F10 `4723ad59d` | - | none | −8 queries on detail pages |
| 8 | Nav menu built once per request | F12 `b9518f7d6` | - | none | −24% device detail, −25% rack detail |
| 9 | Defer cable path rebuilds on REST cable create | F48 `2360dc3ca` | - | none | −18 queries on a two-ended cable create |
| 10 | Nested serializer reuse | F9 `1226fbfa6`, F26 `e5c0b8a21` | 5 | none | −46% interfaces at depth 1; −14% for peer cable paths |
| 11 | REST optimizer: lazy natural-key fallback, prefetch depth-0 FKs, GFKs | F1 `ea00582e7`, F62 `eeec3f094`, F63 `9e83e889d` | 5 | F62 has one | −25% interface list; Device widens 32 -> 212 columns at depth 0 for columns DRF never reads; ~1 query per row per GFK |

PR 11 changes the optimizer every REST list endpoint goes through, and F62 is the change behind the
read screen's "133 worse" query counts: a prefetch replacing a JOIN is more queries, each cheaper. The
description has to say that up front, with database time alongside query count.

### Wave 3: caches and work on next

| PR | Change | Findings and commits | Needs | Tests today | Evidence |
|---|---|---|---|---|---|
| 12 | Natural-key lookup caching | F4 `f219de2bb`, F5 `0db9677b0`, F13 `1ed699aab`, F14 `3dbf87d0e`, F42 `87b372551` | 7 | cache-scope part of `b2d264406` | −89% Constance reads per create loop; −28% bulk create; −48% interface list under load |
| 13 | Detail-page components rendered once; job-modal Job memoized | F3 `7d945a044`, F8 `c2489bf49`, F89 `f973a6bef` | - | none | −186 queries across two detail pages; −46 on detail pages; job lookup 2 -> 1 |
| 14 | Route-shape memoization for `reverse()` | F67 `ae9528392`, F68 `641333c66` | 12 | none | 1,031 reversals on a 100-row page, 78ms in `reverse()`; 542 -> 6 |
| 15 | Table display prefetches | F72 `2519f6563`, F74 `ef05d9f80` | 2 | none | 1.000 query per row on `DeviceTable.device_type`, 0 after |
| 16 | Static choice list rendered once per process | F85 `bc9a18012` | - | has one | byte-identical output against Django's own `Select.render` |
| 17 | Tree display and config without Redis round-trips | F6 `3589b9d2c`, F64 `3441140ae` | 7 | F64 has one | −27% location list; 1 cache GET per row, 97 -> 5 |

PR 17 applies to develop too. It is placed here because it is tier B2, a cache that outlives the
request and has to be invalidated, and the maintainers should see the smaller PRs first. F89 is in PR
13 because it does not apply without F3; it is not the small first PR suggested earlier.

### Wave 4: open an issue and agree the approach before writing a PR

| Change | Findings and commits | Branch | Why discussion first |
|---|---|---|---|
| Caching TemplateColumn | F7 `e35b06b0e`, F11 `f316aaeff`, F54 `cf0d21f22`, then F78 `bf7650927` | develop (F54's test needs porting) | Reimplements django-tables2 internals. F78 needs F11 and PR 6. The coupling tests in `b2d264406` belong here. |
| Empty querysets skip SQL compilation | F55 `571c4a3fc` | next | Depends on Django `QuerySet` internals. |
| Navbar favorites and sidenav fragment caches | F51 `9e6cad9cc`, F58 `b11a78a48` (needs PR 8) | next | F58 caches per user and permission set, so a stale entry is a permissions bug. |
| Stop writing `ObjectChange.object_data` | F22 `d03e0a548` with `03c577a87`, `af568d6d3` | next | Changes what the changelog stores, with a migration. |
| Filter drawer fetched on open | F86 `f0e4e7d07`, `fc5f4ef22` | next | Changes page behaviour. |

## Not in the plan

- Findings 90, 91 and 92: held off `perf/recommended`; full suite not run; finding 92 has an open
  question on devices at depth 1.
- `595395f57` and its revert `036afa90f`: net zero, superseded by `f0e4e7d07`.
- `e0d799f74`: an import-order fixup from a rebase; ruff on each PR branch replaces it.
- `b2d264406`: tests, split between PR 12 (natural-key cache scope) and the TemplateColumn discussion
  (django-tables2 coupling) by what each test covers.

## Per-PR checklist

1. Issue opened and accepted, or the umbrella issue agreed.
2. Branch from the target, cherry-pick the listed commits, squash to one commit per finding.
3. Add the query-count test that fails on the target branch and passes on the PR.
4. Add `changes/<issue>.fixed` (`.changed` for the wave 4 behaviour changes).
5. `invoke ruff`, `invoke pylint`, and the unit tests of every app the PR touches, on the target branch.
6. Description: the problem as a per-row or per-page cost, before -> after with absolute values, how to
   reproduce with the added test, and the tier's adoption cost in one line.

## Drift

On 2026-10-27 both perf branches were rebased onto `upstream/next` at `33ea108ce`. F76 and F77 (PR 6)
conflicted with upstream #9632 in `ipam/tables.py`, on imports only, and keep upstream's. Their develop
versions are unchanged, so the import difference still falls on the merge forward. Re-run the group check
before opening each PR.
