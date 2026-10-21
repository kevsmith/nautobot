# Upstream PR plan for perf/recommended

Drafted 2026-10-20 from `perf/recommended` at `f6a4363d0`, checked against `upstream/develop` at
`72ad4e75c` (3.2.8a0) and `upstream/next` at `4c38263d4` (3.3.0a0).

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

On `origin` (`kevsmith/nautobot`), each based on `upstream/develop` at `72ad4e75c`. None has a PR yet.
A branch without an issue carries `changes/XXXX.fixed.md`, to be renamed to the issue number, and its
name gains the issue number once there is one.

| PR | Branch | Head | Commits | Verified on 3.2 |
|---|---|---|---|---|
| 1 | `u/kevsmith-9668-device-list-primary-ips`, issue [#9668](https://github.com/nautobot/nautobot/issues/9668) | `597bf2192` | F53. F69 from the plan's PR 1 is not on it yet. | yes: test fails without, passes with; `dcim.tests.test_views` 2,486 OK |
| 2 | `u/kevsmith-table-column-per-row-queries` | `2c750e2b8` | F70 `9a2699805`, F71 `9004d443a`, F73 `2c750e2b8` | yes: all 3 tests fail without, pass with; `core.tests.test_tables` 20 OK |
| 3 | `u/kevsmith-rest-viewset-prefetches` | `45abe0d0f` | F25 `e172d4b6c`, F65 `5957277ef`, F66 `45abe0d0f` | yes: all 4 tests fail without, pass with; `dcim.tests.test_api` 1,692 OK, `extras.tests.test_api` 1,479 OK |

Each commit pairs a fix with a test that should fail without it. Commits ported from Ken Celenza's fork
(F65, F66, F70, F71, F73) credit him with `Co-authored-by`. "Verified" means the new tests fail with the
fixes reverted and pass with them, and the touched test modules pass in full, on a 3.2 image built from
`develop` on albert, 2026-10-20.

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
| 1 | Device list primary IPs, LLDP neighbors tab | F53 `c71fbd53a`, F69 `03520cbd7` | - | F53 has one; F69 none | 100 IP-address queries on a 100-row device list from one property; LLDP tab 875 queries on one page, 17.9 per interface |
| 2 | Per-row queries in shared table columns | F70 `792f10d1c`, F71 `d04b27833`, F73 `a0a6e930d` | - | none | 1.000 content-type query per row (ContentTypesColumn); 1.000 per row on to-many accessors; 4.200 per row on relationship columns |
| 3 | Missing prefetches on REST viewsets | F25 `7603c15af`, F65 `8ea98df5c`, F66 `13032aed5` | - | none | parent_bay join −5.8% on interfaces at depth 1; 1.000 query per row on front/rear-port cable peers and on Job task queues, 0 after |
| 4 | Home page changelog panel | F56 `f1b1d3a58` | - | has one, which conflicts on develop and needs porting | 25 of the home page's 79 queries are one panel rendering 15 rows |

### Wave 2: query fixes in shared code, on develop

| PR | Change | Findings and commits | Needs | Tests today | Evidence |
|---|---|---|---|---|---|
| 5 | Fetch each cable path once; prefetch connection endpoints | F2 `385137620`, F30 `e3e4829a3`, F34 `1e549356d`, F36 `626903965` | - | none | interface-connections −61% at limit 50; power-connections −51%; cable terminations −76% |
| 6 | Table queryset seam and per-table batching | F75 `3380fd708`, F76 `d71e9f119`, F77 `0914abf5d` | - | none | 3.000 queries per row on prefixes and 1.973 on locations, both to 0; 55 offending columns across 4 tables |
| 7 | Ancestor walks in blocks | F10 `ef9dbd58c` | - | none | −8 queries on detail pages |
| 8 | Nav menu built once per request | F12 `502b4daaf` | - | none | −24% device detail, −25% rack detail |
| 9 | Defer cable path rebuilds on REST cable create | F48 `325720c4f` | - | none | −18 queries on a two-ended cable create |
| 10 | Nested serializer reuse | F9 `f99bccf91`, F26 `a33648b13` | 5 | none | −46% interfaces at depth 1; −14% for peer cable paths |
| 11 | REST optimizer: lazy natural-key fallback, prefetch depth-0 FKs, GFKs | F1 `f1097bf48`, F62 `feaa0f2e4`, F63 `e553791fd` | 5 | F62 has one | −25% interface list; Device widens 32 -> 212 columns at depth 0 for columns DRF never reads; ~1 query per row per GFK |

PR 11 changes the optimizer every REST list endpoint goes through, and F62 is the change behind the
read screen's "133 worse" query counts: a prefetch replacing a JOIN is more queries, each cheaper. The
description has to say that up front, with database time alongside query count.

### Wave 3: caches and work on next

| PR | Change | Findings and commits | Needs | Tests today | Evidence |
|---|---|---|---|---|---|
| 12 | Natural-key lookup caching | F4 `7b0e72a7f`, F5 `4c9f0007b`, F13 `a6e51aba1`, F14 `e91e8a1a1`, F42 `f32f0c2ce` | 7 | cache-scope part of `1952f6720` | −89% Constance reads per create loop; −28% bulk create; −48% interface list under load |
| 13 | Detail-page components rendered once; job-modal Job memoized | F3 `35a844587`, F8 `b060364a1`, F89 `54f8a335d` | - | none | −186 queries across two detail pages; −46 on detail pages; job lookup 2 -> 1 |
| 14 | Route-shape memoization for `reverse()` | F67 `372616eca`, F68 `443343ea8` | 12 | none | 1,031 reversals on a 100-row page, 78ms in `reverse()`; 542 -> 6 |
| 15 | Table display prefetches | F72 `63b8d69be`, F74 `4f68806eb` | 2 | none | 1.000 query per row on `DeviceTable.device_type`, 0 after |
| 16 | Static choice list rendered once per process | F85 `e8fdc35d9` | - | has one | byte-identical output against Django's own `Select.render` |
| 17 | Tree display and config without Redis round-trips | F6 `19a575938`, F64 `fde9defaa` | 7 | F64 has one | −27% location list; 1 cache GET per row, 97 -> 5 |

PR 17 applies to develop too. It is placed here because it is tier B2, a cache that outlives the
request and has to be invalidated, and the maintainers should see the smaller PRs first. F89 is in PR
13 because it does not apply without F3; it is not the small first PR suggested earlier.

### Wave 4: open an issue and agree the approach before writing a PR

| Change | Findings and commits | Branch | Why discussion first |
|---|---|---|---|
| Caching TemplateColumn | F7 `54df8bf60`, F11 `ab71c726a`, F54 `26586c397`, then F78 `061304cb6` | develop (F54's test needs porting) | Reimplements django-tables2 internals. F78 needs F11 and PR 6. The coupling tests in `1952f6720` belong here. |
| Empty querysets skip SQL compilation | F55 `5b406a212` | next | Depends on Django `QuerySet` internals. |
| Navbar favorites and sidenav fragment caches | F51 `2c7324e59`, F58 `1e5675570` (needs PR 8) | next | F58 caches per user and permission set, so a stale entry is a permissions bug. |
| Stop writing `ObjectChange.object_data` | F22 `3dc30e6e1` with `ab4a27f9d`, `f6a4363d0` | next | Changes what the changelog stores, with a migration. |
| Filter drawer fetched on open | F86 `f6b8d293e`, `1f41edf4b` | next | Changes page behaviour. |

## Not in the plan

- Findings 90, 91 and 92: held off `perf/recommended`; full suite not run; finding 92 has an open
  question on devices at depth 1.
- `301b0002d` and its revert `9c4183669`: net zero, superseded by `f6b8d293e`.
- `7253f6a5a`: an import-order fixup from a rebase; ruff on each PR branch replaces it.
- `1952f6720`: tests, split between PR 12 (natural-key cache scope) and the TemplateColumn discussion
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

`upstream/next` has moved 18 commits since `perf/recommended` was rebased. F76 and F77 (PR 6) now
conflict with upstream #9632 in `ipam/tables.py` on `next`; they apply to `develop`, so the conflict
falls on the merge forward. Re-run the group check before opening each PR.
