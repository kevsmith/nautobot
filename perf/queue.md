# Work queue

As of 2026-09-13. Ordered, with the reasoning that produced the order — so the
sequence can be argued with rather than just followed. **Next up is at the top;
context and closed items are below it.**

**Branch state.** The baseline moved on 2026-09-13: `next` is now `upstream/next` at
`c3605ae48`, 98 commits on from the `ce01a0464` every figure before that date was
measured against. `perf/experiments` carries the harness and the records;
`perf/recommended` at `0e3efb8ab` is the upstream-facing branch — linear on `next`,
zero merges, 48 commits, 63 files `+3,022 −454`, adding no file outside `nautobot/`,
and byte-identical to `perf/experiments` under `nautobot/`. 80 findings recorded, 64
accepted, of which 15 are instruments or measurement results rather than product
changes, leaving 49 accepted product changes.

The last completed full suite run was **17,952 tests, `OK (skipped=666, expected
failures=1)`**, run serially on the rebased tree. See "Done: the serial full suite is
green" below, which records both runs.

**What is preserved, and why it has to be.** Both branches were rebased, so every SHA
they carried changed. Three tags hold the old state:

| tag | commit | what it is |
|---|---|---|
| `perf/baseline-ce01a0464-experiments` | `77ffb74eb` | `perf/experiments` as published against the old baseline |
| `perf/baseline-ce01a0464-recommended` | `977eb44c6` | `perf/recommended` as published against the old baseline |
| `perf/archive-verified` | `02d04700f` | `perf/verified`, deleted once superseded |

Findings' `commit:` and `recommended_commit:` fields were repointed to the rebased
SHAs. SHAs appearing in finding *prose* were not: they describe what was measured at
the time, and the tags are what keep them resolvable. `perf/verified` is named in
finding 39 as a measurement arm and renders into `methodology.md`, which is why it is
a tag rather than simply gone.

---

## Next up

### 1. `?depth=2`, the largest unexplored surface on the read side

**No finding on this branch has ever measured a depth-2 endpoint.** `workload.yml` names depth
0 and depth 1 only, so the read loop cannot see this, and `screen_reads.py` enumerates
`list.depth1` as its deepest kind. Measured 2026-09-12 while assessing the imported series, all
still open:

| endpoint | cost | note |
|---|---|---|
| `/api/circuits/circuits/?depth=2` | **28.0 q/row** (710 at 25 rows) | The imported series claims ~1,100 -> ~340 here; ours did not move, so the cause differs from theirs |
| `/api/dcim/interfaces/?depth=2` | **14.84 q/row** (1,491 at 100 rows) | Untouched by everything the branch has done; finding 79's enrichment left the slope unchanged |
| `/api/dcim/cables/?depth=1` | **6.027 q/row** (611 at 100 rows) | A Cable's nested termination serialization walks each termination's parent: 200 `dcim_cablepath`, 199 `dcim_device`, 173 `dcim_interface` on one page. `Cable` is not a `CableTermination`, so finding 79's rejected commit does not reach it |
| `/api/dcim/console-ports/?depth=1` | **1.000 q/row** | Pre-existing; found while measuring finding 79 |

Why this is first:

- **It is the only read surface with a measured per-row slope and no finding against it.**
  Everything else in this queue is either done, ranked on data that omits it, or per-row by
  design. The UI screen has now run and added exactly one candidate, not a field of them.
- **Finding 62 does not reach it.** That change prefetches FK fields at `?depth=0`, where a
  `RelatedField` never reads the joined columns. At `depth>=1` the nested serializer renders
  the whole related object, so the JOIN stays and both `depth>=1` rows in its own by-action
  table are flat by construction.
- **28 q/row is the worst slope on record here.** For comparison, the read loop's heaviest
  scenario, `api.interface.depth1`, now runs 1,162 ms against stock's 6,992.

First step is cheap and settles the shape: add depth-2 scenarios to `workload.yml`, then
`probe_query_slope.py` with `PERF_SHAPES=1` at two page sizes to get the slope and the
repeated query shapes behind it.

### 2. The per-row utilization aggregates

The residue of the column audit, and the one place both this branch and the imported series
stopped by design:

| column | cost |
|---|---|
| `PowerFeedTable.utilization` | 8.0 q/row |
| `RackDetailTable.get_power_utilization` | 3.0 q/row |
| `PrefixDetailTable.utilization` | 2.0 q/row |

These are genuinely per-row rather than repeated work, which is why the table series did not
take them: each row's utilization is a distinct aggregate over that row's children. Taking them
means computing the aggregate set-wise and attaching it to the queryset, not memoizing a
repeated call — a different shape of fix from findings 76-79, and the reason it is its own
entry rather than their leftover.

Four further columns of the audit's remaining 18 are the audit's own blind spot: it does not
call `paginate()`, so it misses the hierarchy prefill that makes `/ipam/prefixes/` read 17
queries flat. Those need no fix, only a correction to the audit.

---

## Queued: `perf/ez-review`, a review-ordered cut of `perf/recommended`

**Not started. Agreed 2026-09-13.** `perf/recommended` is ordered for *attribution* — commits
appear in the order the changes were made, and each maps to exactly one finding, which is what
`recommended_commit` records and what the report's table cites. That ordering is poor for
review. A separate branch, `perf/ez-review`, would carry the same tree in an order a reviewer
can work through, leaving `perf/recommended` as the attribution branch.

**SHAs below are valid at `perf/recommended` `0e3efb8ab`.** If that branch moves, re-derive
them by subject rather than trusting these.

### Step 1 — delete the no-op pair, which is the largest single win and is not about ordering

Two commits cancel exactly:

| | commit | |
|---|---|---|
| adds | `c2c211cd1` | Fix the tag cache in `serialize_object`, which never fires |
| reverts | `e6b496057` | Revert finding 31: its tag cache has no reader since finding 22 |

**Verified, not assumed:** all four touched files — `core/models/utils.py`,
`extras/api/mixins.py`, `extras/context_managers.py`, `extras/signals.py` — are byte-identical
at `c2c211cd1~1` and at `e6b496057`, and no commit between the two touches any of them. A
reviewer currently watches a change be added and removed for no net effect. 48 commits become
46 by deleting both.

### Step 2 — move the state-changing commits into a labelled tail

| commit | tier | what |
|---|---|---|
| `d6a786e30` | `C` | Stop writing `ObjectChange.object_data` — carries migration `0146` |
| `148b2df32` | `B2` | Stop paying Redis round-trips for tree display and config |
| `4f0382393` | `B2` | Resolve the navbar-favorites URLs once per process |
| `42f25eccc` | `B2` | Cache the rendered sidenav item fragment per (user, theme) |

**Every one is already isolated:** no later commit on the branch touches any file any of them
touches, so moving them costs nothing semantically.

**This is the property worth having.** A maintainer could take the first 42 commits and defer
the tail without rebasing anything. Today, deferring the `object_data` migration means rebasing
the 27 commits behind it.

### Step 3 — group the middle by subsystem

`core/api` 7, `core/models` 8, `core/tables.py` 6 plus its rollouts into `dcim`/`tenancy`/`ipam`,
`dcim` 12, `extras`, `core/views`, `core/forms`, `core/templatetags`, `core/context_processors`.
Splittable across reviewers. The tables chain is already in dependency order — the
`TemplateColumn` compilation change first, the `BaseTable.replace_queryset` extraction last as
cleanup — so grouping it reorders nothing semantically.

### Step 4 — attach the tests-only commit to what it tests

`66f14aa16` ("Add the cache-scope and django-tables2 coupling tests") sits at position 25 and
tests findings 07 and 11, at positions 7 and 11. Upstream review generally wants tests with the
change they cover.

### What it costs, and what has to be re-recorded

- **Attribution breaks.** All 47 `recommended_commit` fields need repointing. The tooling
  exists and was used twice on 2026-09-13: build an old→new map from `rev-list --reverse` on
  both sides, verify 1:1 by subject with zero mismatches, then substitute on the
  `recommended_commit:` line alone — never a yaml round-trip, the finding files carry folded
  scalars it would reflow.
- **Findings 31 and 60 lose their commit entirely**, because the branch would no longer carry
  that change. That is more honest than the current state and the finding files should say so
  rather than going silent.
- **Decide whether `recommended_commit` should point at `perf/recommended` or at
  `perf/ez-review`.** It cannot point at both, and `verify_report.py` asserts every SHA in the
  report's table is on the branch it names.

### How to build it safely

Build in a worktree; tag the old tip first, as with the two re-baselining rebases. **The gate
before it replaces anything:** `nautobot/` byte-identical to `perf/experiments`, the net diff
against `next` textually identical to `perf/recommended`'s (63 files, `+3,022 −454`, 3,476
changed lines), zero merges, and nothing added outside `nautobot/`. A reordering that changes
the tree is a reordering that went wrong.

## Done: the UI surface is screened (finding 81)

This headed "Next up" until 2026-09-13, when `perf/scripts/screen_ui.py` was written and run.
127 UI list views, 249 measurements, 119 seconds. It measures **both** requests a list view
makes -- the document, which builds its table over `queryset.none()`, and the `HX-Request`
follow-up that renders the rows -- because measuring either one alone is a recorded way to be
wrong.

**The rows are flat.** 37 of the 38 tables rendering a full page sit at or below 1.0 queries per
rendered row: `dcim:device_list` 8 queries for 25 rows, `ipam:ipaddress_list` 8,
`dcim:cable_list` 9. That is findings 07, 11, 53, 54 and 76-79 landing, and it closes the
row-scaling question this queue opened with.

**One outlier, and it is the only one.** `extras:relationshipassociation_list` reads **58 queries
for 25 rows**, 2.32 per row, stable across three reps, against a field where nothing else exceeds
1.00. Queued immediately below.

**What is left is a fixed cost rather than a per-row one.** The document request is
near-constant: median 90.1 ms and 10 queries, range 19.3-187.9 ms, independent of table size
because it renders no rows by construction. Over the 38 populated tables the split is 40.9%
document to 59.1% rows, consistent with finding 44. Over all 124 answering on both it inverts to
52.9% document, because 86 of them render fewer than ten rows and a near-constant cost dominates
a small one. Quoting that inverted figure as though it described a list view a user waits on
would overstate the document.

**This entry existed to make the read-versus-write ordering honest, and it does so by pointing
somewhere else.** The read side's remaining cost is the fixed page, which "The fixed cost of a
page" below already attributes to template rendering rather than to the view. There is no field
of UI per-row defects to rank against the write path.

### Queued from it: `extras:relationshipassociation_list` at 2.32 q/row

58 queries for 25 rows, stable across three reps. The only per-row defect the whole UI screen
found. Not yet attributed. `attribute.py` against the row request is the first step, and the
column audit's blind spot is worth ruling out before anything else, since that audit does not
call `paginate()`.

## Done: the row-rendering request on every list view (findings 44, 76-79)

This headed "Next up" until 2026-09-13, under a numbering since superseded. It opened because finding 44 showed the harness had never
issued the XHR that renders a list view's rows, so an entire request class was unprofiled. The
18 `ui.*.list.rows` scenarios now carry `HX-Request`, and the table work that followed took
them apart:

| scenario | before | after |
|---|---:|---:|
| `ui.prefix.list.rows` | 310 q | 16 q |
| `ui.prefix.list.page100.rows` | 311 q | 17 q |
| `ui.location.list.rows` | 205 q | 9 q |
| `ui.role.list.rows` | 53 q | 8 q |
| `ui.circuit.list.rows` | 45 q | 8 q |
| `ui.status.list.rows` | 30 q | 8 q |

Finding 11's `wall_clock` field no longer reads "not measured"; the row requests are in the
read loop and carry real numbers. What it left behind is item 2 above.

---

## Candidates — not yet ranked

Unranked deliberately: the first depends on a contradiction nobody has resolved,
and the second depends on the first's answer.

### The fixed cost of a page — answered, and it is template rendering

**Resolved by finding 50.** This entry used to carry two readings that disagreed about
whether a page's cost is chrome or per-model machinery, and asked for a quiet-box re-run
before ranking. That re-run happened.

Chrome was **64–66ms** when this was written (`ui.chrome.404` 66.3ms, `ui.search` 64.7ms)
against `ui.device.list` at **283ms**, so the per-model term was ~218ms and dominated by 3.4×.
**Both halves have since moved and the ratio moved with them** (finding 82, 2026-09-13):
chrome is now **19.6ms** and `ui.device.list` **181ms**, so list-view machinery is ~161ms and
dominates by 8×. Findings 51, 52, 57 and 58 took roughly 70% out of chrome; `inc/nav_menu.html`
is 0.7–1.3ms exclusive, from ~20.4ms. **The target is list-view machinery, and it is now the
target by a much wider margin than this entry originally claimed.**

What the 218ms is made of, with the instrument validated first (+1.6% overhead; stubbing
a template removes 45.6ms against 48ms attributed):

| | |
|---|---|
| view work | **21.4ms** — filtersets, tables, permissions |
| template rendering | **261.1ms — 91.6% of the request** |
| database | 4.3ms / 9 queries |
| Redis | <1ms / 18 reads |
| off-CPU | 5.0ms total, 29 voluntary context switches |

So the candidates this entry used to name — filter-form construction, table-column
construction over an empty queryset, saved-view resolution — are all **in the view**, which
is 7.6% of the page. They are not the cost. Rendering is.

*Ranked by size, with names:*

1. **~0.6ms to render one table cell, and one column is 37% of it.**
   `ui.device.list.rows` emits 1,000 `<td>` at 10 per row for 487.3ms of cell rendering,
   ~64% of a 747ms request. Attributed per column class by
   `perf/scripts/probe_table_columns.py`:

   | column | class | linkify | ms/cell | total |
   |---|---|---|---:|---:|
   | `primary_ip` | `Column` | True | **1.789** | 178.9ms |
   | `actions` | `ButtonsColumn` | | 0.765 | 76.5ms |
   | `tenant` | `TenantColumn` | | 0.706 | 70.6ms |
   | `location` | `Column` | True | 0.361 | 36.1ms |
   | `name` | `TemplateColumn` | | 0.280 | 28.0ms |
   | `device_type` | `LinkColumn` | True | 0.267 | 26.7ms |
   | `pk` | `ToggleColumn` | | 0.238 | 23.8ms |
   | `role`, `status` | `ColoredLabelColumn` | | 0.216 | 43.1ms |
   | `rack` | `Column` | True | 0.036 | 3.6ms |

   **`linkify` is not the cost** — True totals 245.3ms against False at 242.0ms, and the
   True side is almost all one column; `rack` is the same class with the same linkify over
   a real FK at 50× less. **Findings 07 and 11 hold** — `TemplateColumn` is 0.280ms/cell,
   the caching subclass working. **Do not go looking in `inc/table.html`**; it is 105 lines
   of ordinary markup and django-tables2 work lands on whichever template executes.
   Recurs elsewhere: 93.9ms over 6 renders on rack detail, 17.3ms over 7 on device detail.
   `role_retrieve.html` includes it **eighteen times** and is not in the workload. Only 10
   of `DeviceTable`'s ~22 declared columns are visible by default, and `capabilities` and
   `manufacturer` are the same accessor-on-a-property shape as `primary_ip`.
2. **`api.device.list` — 441ms of database across 8 queries**, ~55ms each, no templates at
   all. A different problem from everything else here.
3. **`inc/nav_menu.html` — CLOSED, and the remainder is interpretation cost.** Findings 12, 50,
   51, 52 and 57 took it from ~47ms to **~20.4ms**, and finding 57 attributed what is left one
   level below the per-template split: 160 compiled nodes expanding to **3,629 node renders** at
   ~7us each. VariableNode (1,160 renders, ~7.5us) and IfNode (511, ~8.9us) are 65% of it, and
   those are Django resolving variables and evaluating conditions. `URLNode` is down to 3 renders,
   which is finding 51 confirmed by a second instrument.

   **An implementation plan for the caching route is written up in
   [`perf/nav-menu-cache-plan.md`](nav-menu-cache-plan.md)** — key shape, the five phases, the
   tests that make it safe, and a projected −18ms per chrome-bearing request recorded as a
   prediction to check against.

   **There is no hot spot, so the only levers are fewer nodes or fewer renders.** Fewer nodes is a
   product decision about menu size. Fewer renders means not rendering it per request, and the
   interesting shape there is that **the data is already in the page twice**: `inc/javascript.html`
   emits the whole menu as JSON (12,841 bytes, item 5 below), so the browser gets a rendered HTML
   menu costing ~20.4ms of server time *and* a JSON copy of the same structure. Caching the HTML
   instead is risk B2 with a permission-set cache key, which finding 52 showed is not uniform even
   between two users who see nearly the same menu. Neither is an experiment this branch can run as
   scoped; both are design changes.

4. **The filter drawer — and it is now the largest fixed cost on the read side.**
   Re-measured by finding 82: **36.2ms of 181ms on `ui.device.list` (20%)** and **46.0ms of
   143ms on `ui.prefix.list` (32%)**, on a drawer that is closed until clicked. Two costs inside
   it, and they are separate levers:

   - **One field.** `prefix_length` is a single `StaticSelect2` with **133 options for 26.1ms**,
     64% of that page's widget time. `ipaddress_list` is the same shape with 141. Ranked
     separately below as "one filter field renders 130 invisible options".
   - **Empty API-backed selects, which nobody had costed.** `APISelectMultiple` renders *no*
     options and still costs **~400us each** — 19 of them for 7.6ms on the device list, 12 for
     4.8ms on the prefix list. The cost is attribute rendering, not options:
     `attrs.html` renders 156 times on the device list and 207 on the prefix list. This scales
     with how many filter fields a model has, where the 133-option field scales with how many
     choices one field offers.

   Finding 81 makes this a surface-wide number rather than a two-page one: the document is a
   near-constant 90ms median across **127** UI list views.
5. **~7.6KB/page of discarded JSON.** `inc/javascript.html` re-emits the whole menu via
   `json_script` (12,841 bytes, 3.2% of a 401KB page); `ui/src/js/search.js:26` reads it but
   flattens tabs and groups away and uses only `item_link -> name` (5,231 bytes). Costs at
   most 2.3ms, so this is a payload item rather than a time item.

*Two things worth knowing before picking one.* On the detail pages the queries are **not in
the view** — `ui.device.detail` fires 49 of 52 inside template rendering, `ui.rack.detail` 60
of 63 — so prefetching there has nothing to attach to. And `is_active` is per-request state
inside both the menu HTML and its JSON, which is the obstacle to caching either.

### Client-side asset loading, which no instrument here can measure

`inc/javascript.html` loads five scripts immediately before `</body>` in
`base_django.html:63`, all plain `<script src>` with no `defer` or `async`:

    1,571,177 bytes  dist/js/libraries.js
       41,995 bytes  dist/js/nautobot.js
       14,545 bytes  js/forms.js
        1,040 bytes  js/dropdown.js
           92 bytes  js/table_sorting_indicator.js

    8,376,358 bytes  dist/js/libraries.js.map   (tracked in git, not served)

**libraries.js is 1.5MB, and 78.8% of it is a charting library.** Attributed from its own
source map — 668 source files, 5,640,288 bytes of pre-minification input:

| bytes | share | package |
|---:|---:|---|
| 3,818,276 | 67.7% | `echarts` |
| 624,990 | 11.1% | `zrender` (echarts' renderer) |
| 285,314 | 5.1% | `jquery` |
| 168,655 | 3.0% | `htmx.org` |
| 153,585 | 2.7% | `select2` |
| 135,902 | 2.4% | `bootstrap` |
| 108,219 | 1.9% | `flatpickr` |
| 91,001 | 1.6% | `highlight.js` |

A full charting library — canvas and SVG renderers, coordinate systems, dozens of chart
types — ships on every page including a 404 and a zero-row list. The mechanism is one
webpack line: `splitChunks: {chunks: 'all', name: 'libraries'}`. Naming the chunk forces
all shared code into a single file, so nothing can load conditionally. GraphiQL and React
escaped only because they are built by a separate config with their own entry; ECharts did
not. That makes the loading strategy secondary — `defer` rearranges *when* 1.5MB arrives,
where the real question is whether ~1.2MB of charting needs to arrive on pages with no
chart. That is code splitting (a dynamic `import()` at the chart call site, or a second
entry as GraphiQL already has), not an attribute on a script tag.

Because the tags are synchronous and ordered, libraries.js also blocks the three after it —
including `table_sorting_indicator.js`, which is **92 bytes** and gets its own blocking
request.

*Correction:* an earlier revision of this entry said 4.25MB, and the commit that added it
(`d5da3b411`) repeats that figure. It came from reading `ls -s` block counts as bytes, which
also inflated the 92-byte file to "8 KB". The byte figures above are from `stat`. The
proportions are pre-minification source shares, so the shipped proportion may differ —
tree-shaking and minification do not compress every library equally, and confirming it needs
the bundle inspected rather than its map. `defer` would let them download in parallel and execute in order after
parsing. `rel="preload"` buys little at that position — the document has already streamed
by the time the parser arrives — and `rel="prefetch"` is the wrong relation entirely: it is
for resources a *future* navigation will need, at lowest priority, and on a page's own
scripts it is ignored or actively pessimising.

*Why this is unranked and cannot be ranked.* **Every instrument in this harness measures
server-side response generation.** Tier 1 drives Django's test client, Tier 1W runs
in-process, Tier 2 is cassowary measuring HTTP response time. None of them parse HTML or
execute JavaScript, so `defer`, `preload`, and a 4.25MB bundle move **zero** of the numbers
in this report — including finding 51's −43%. There is no basis on which to compare this to
anything else in the queue.

*Next step is an instrument, not a change.* The demo-video workload recorded as future work
in `perf/README.md` is the seed of it: a browser-timing instrument would be the first thing
here able to measure what a user experiences rather than what uwsgi emits, and it is the
prerequisite for pricing this at all.

### CLOSED, and it was measured wrong: a Constance read costs 2.4us in a request

This entry claimed a memoized Constance read costs 224us and that `ui.device.list.rows`
paid it 204 times, so ~45ms per request with app-wide reach. **That conclusion was an
artifact of measuring outside a request**, and finding 53 corrected it. Kept rather than
deleted, because the mistake is more instructive than the entry ever was.

The original table, all of it measured standalone:

    get_settings_or_config('PREFER_IPV4')       224.5 us/call
    100x device.primary_ip, select_related       24.11 ms   241.1 us/access
    100x device.primary_ip, no select_related   153.84 ms  1538.4 us/access
    100x the config call alone                   23.71 ms   237.1 us/access
    => config is 98% of the select_related case   <-- wrong, see below

The 224us reproduces: 211-219us per call, stable to n=10,000. But measured *inside* the
request that actually renders the page:

    in-request calls                              200
    total                                        0.94 ms
    first call (cache fill)                    489.46 us
    calls 2..200, median                         2.38 us

`nautobot/core/utils/config.py` carries `get_request_cache` and
`_invalidate_request_cached_config`. The cache is per-request, not per-process — the
warm-up request did not make the measured request's first call cheap — so every caller
after the first in a given request pays 2.4us. The whole per-request cost of `PREFER_IPV4`
on the heaviest page here is about **1ms**.

Three consequences:

- **"Cache `PREFER_IPV4` per request" was the proposed fix. It already exists**, and it is
  why the cost is 1ms. The proposal was chasing a number measured in a context that does
  not occur when serving.
- **"Config is 98% of the select_related case" is false in a request.** Re-attributing
  `primary_ip` on the fixed tree puts the whole column at 39.8ms with 4.3ms of accessor
  time — so the 137.1ms of accessor cost finding 50 recorded was the N+1 queries, which
  finding 53 removed, and never the config read.
- **The lesson generalises past this entry.** A micro-benchmark of a function that consults
  request-scoped state measures the uncached path by construction. Anything on this branch
  priced by calling a function in a bare loop deserves re-checking inside a request before
  it justifies a change.

What survives: `api.location.list` making 101 of these calls (finding 06) is not obviously
a problem either, and nobody has measured it in-request. Cheap to check, and the same
correction probably applies.

**The query half is closed by finding 53.** The `no select_related` row above was the live
state of the device list, not a hypothetical: the list action select_relates both FKs as of
5f8c5f643, so `ui.device.list.rows` goes 107 -> 7 queries and −14.3% wall. Nothing is left
to do here — the config half is the ~1ms measured above, and there is no property-level fix
worth writing for a 2.4us call. `primary_ip` being defined three times (Device at
dcim/models/devices.py:1006, VirtualDeviceContext at 2330, and VirtualMachine) matters only
for the select_related blind spot below, not for the config read.

### One filter field renders 130 invisible options

`ui.prefix.list` spends **19.8ms rendering 138 `<option>` elements**, and 130 of them are a
single field:

    130 options  StaticSelect2            prefix_length
     39 options  Select                   form-0-lookup_field
     18 options  SelectMultipleOrderable  columns
      3 options  StaticSelect2Multiple    type
      3 options  StaticSelect2            ip_version

`prefix_length` offers `range(0, 129)` plus a blank: `PREFIX_LENGTH_MAX` is 128, sized for
IPv6, so IPv4's 0-32 is a subset of that range rather than a second range added to it.
`ui.ipaddress.list` is the same shape as `mask_length`, `range(1, 129)` plus a blank, 129
options on a `StaticSelect2Multiple`, measured at 25.4ms (finding 82). Both land inside the filter
drawer, which is closed until clicked — so this is ~27ms per page spent on markup nobody sees
until they open the drawer, and most users never do.

Two ways out, and they are the same two as the drawer entry below: **render it lazily**, or
**do not render 130 options** (a number input with validation, or an API-backed select). Both
change behaviour; neither is a rendering optimisation. Worth ~27ms on two pages.

*This entry exists because the aggregate misled.* 19.8ms across 138 renders reads like Django
template machinery being slow, and the fix that suggests is a Python option-builder. Attribution
says it is one pathological field. Third time today an aggregate pointed the wrong way, after
the `<string>` templates and the 224us Constance read.

### Priced and not taken: a Python option-builder for SelectWithDisabled

Django's `select.html` renders `{% include option.template_name with widget=option %}` per
option, and Nautobot's `selectwithdisabled_option.html` then includes `attrs.html` itself, so
**every option costs two template renders**. Building that HTML in Python instead would produce
identical output — the format is fixed and reproducible:

    '<option value="a"\n         selected\n        >Plain</option>'

Measured benefit, on the tree with the queryset guards applied:

| page | option renders | saving | share |
|---|---|---:|---:|
| `ui.prefix.list` | 138 + 207 attrs | ~26.8ms | ~15% |
| `ui.ipaddress.list` | 141 + 205 attrs | ~26ms | ~14% |
| `ui.device.list` | 37 + 156 attrs | **~6.3ms** | **~3%** |

**Not taken, and the reasoning still holds for the general version.** It is a whitespace-exact
rewrite of HTML generation for the most widely used widget class in the product, and 94% of the
headline win is `prefix_length` rendering options nobody sees (above). 3% on the main list view
does not justify that risk. Four subclasses override `option_template_name` (`ColorSelect`,
`SelectWithPK`, `ContentTypeSelect`) and would need a fallback.

**A scoped version is a different proposition, and finding 82 measured what it would be worth.**
Rather than rewriting the widget class, precompute the option string for fields whose choices
come from a static constant and splice `selected` in at render time. Every objection above is
about the general case: the subclasses that override `option_template_name` are not these
fields, and whitespace-exactness becomes provable by byte-diffing one `<select>` instead of
auditing a class used everywhere. It captures the 94% that is concentrated in one field.

What makes these two fields safe, all verified: choices are a module-level constant with no
queryset; nothing varies per user; and no option carries attributes, so the options substring is
invariant and only `selected` moves. The split is **0.305ms fixed + 190.4us per option**, so
**98.8% of a 130-option field is the options** -- roughly 24.8ms recoverable per field, ~17% of
a prefix-list or IP-address-list page. `StaticSelect2Multiple` fits the same line, so multiple
selection needs no special case beyond splicing N selected rather than one.

Not yet run. Two things to prove first: byte-identical output across no-filter, single and
multiple selection; and a control page without such a field (`ui.device.list`) that must not
move.

### The queryset guards do not cover non-Nautobot models

The `_fetch_all`/`exists`/`iterator`/`count` guards live on `RestrictedQuerySet`, so they reach
every Nautobot model — and nothing else:

    ContentType        QuerySet            guarded=False
    Group              QuerySet            guarded=False
    ObjectPermission   RestrictedQuerySet  guarded=True

A `DynamicModelChoiceField` over `ContentType` or `Group` therefore still compiles and discards
a query per widget. Nautobot has plenty of content-type-driven forms — object permissions,
computed fields, relationships, custom fields — and **none of them is in the workload**, so the
cost there is unmeasured rather than shown. *Add a scenario for one of those forms first.*

If it turns out to matter, the fix already exists and is measured: the parked
`MinimalModelChoiceIterator` change at **6594107e3** keys off the *field* rather than the
queryset class, so it covers any model. It is 29 lines in form code with no Django-internals
coupling, and it also stands as the lower-risk fallback if the `RestrictedQuerySet` guards do
not survive review — recovering 19 of a device list's 99 discarded compilations.

### The In-lookup residual, and the only safe way to widen the guards

After the four guards, one discarded compilation survives on `api.device.list` and two on
`ui.home`: `filter(pk__in=[])` raises `EmptyResultSet` from the `In` lookup with no
`NothingNode`, so `query.is_empty()` correctly returns False and the guard declines to fire.

**~1.2ms across the whole workload**, which is why this is a note and not a change. Recorded
because the obvious extension is unsafe: an empty `In` under an `OR` does not make a query
empty, and under a negation it makes it match *everything*, so "find an empty `In` anywhere"
would return no rows for a query that should return all of them. The only sound version applies
the same structural discipline `is_empty()` uses — an empty `In` as a **direct child of a
non-negated AND root** — and that buys 1.2ms for a second Django-internals dependency.

### Two more columns still compile their template once per cell

Finding 54 fixed `TenantColumn`; two columns still subclass
`django_tables2.TemplateColumn` directly and so still re-parse their source on every
cell, because `django_tables2`'s `render()` ends in
`Template(self.template_code).render(parent_context)`:

| site | column | measured? |
|---|---|---|
| `nautobot/dcim/tables/template_code.py:6` | `DeviceComponentNameColumn` | no scenario renders it |
| `nautobot/extras/tables.py:1029` | `JobResultColumn` | no scenario renders it |

The fix is the same one line as finding 54 and its equivalence is already covered by
`CachingTemplateColumnCouplingTestCase`. What is missing is the cost: neither column
appears in a workload scenario, and finding 54's own numbers came out at ~0.27ms per
avoided compile, so a table rendering either at 100 rows would be worth ~27ms and one
rendering neither is worth nothing. **Add a scenario that renders each, then fix.**

The payoff beyond the two sites is a structural test: once no shipped column subclasses
`django_tables2.TemplateColumn` directly, that becomes assertable by walking the module
tree, and the next column declared the wrong way fails a test instead of quietly costing a
compile per cell. That guard is worth more than either individual fix.

### The home page's 79 queries: ~25 are one changelog panel, 29 are one count per item

**This entry replaces an earlier version that was wrong.** It said the cost was "resolving
`{{ connections... }}`" in `render_additional_content`. Attribution says otherwise. Line 116 of
`nautobot/core/views/__init__.py` is `return template.render(additional_context)`, and the
queries fire *inside* that render because two of the panel callbacks return **unevaluated
sliced querysets** rather than values:

    41  core/views/__init__.py:116 render_additional_content
          django_content_type x15, dcim_virtualchassis x10, extras_jobresult x5
    29  core/views/__init__.py:154 get
          one count per HomePageItem carrying a model=
     3  dcim/homepage.py  _connected_{interfaces,console_ports,power_ports}_count
     6  misc (session, user, objectchange, tree count)

Full stacks name the mechanism exactly, and it is two separate N+1s in the changelog panel:

    15x django_content_type   related_descriptors.py:261 __get__
                              -> the `changed_object_type` FK, one query per row
    10x dcim_virtualchassis   fields.py:262 __get__ -> get_object_for_this_type
                              -> GenericForeignKey resolution, one query per row

`extras/homepage.py get_changelog()` returns `ObjectChange.objects.restrict(...).only(...)[:15]`,
so rendering 15 rows costs ~25 queries. `get_job_results()` and
`get_approval_workflow_stages()` are the same shape.

*The fix is textbook and the shape already exists on this branch:*
`select_related("changed_object_type")` folds the 15 content-type lookups into the main query,
and `prefetch_related("changed_object")` batches the GFK by content type — Django issues one
query per distinct type instead of one per row. Expect ~25 queries to become ~4. At the home
page's ~0.58ms/query that is **~12ms of a 157ms page**, plus whatever the per-row Python costs.
Two cautions: the queryset uses `.only()`, which does not compose with a GFK and needs checking
against `select_related`; and it is sliced, so the prefetch must run after slicing.

**The 29 counts are not a defect.** One count per dashboard item is linear in items, which is
the right shape for a page whose content is counts. Worth knowing rather than fixing.

*What is still unexplained:* the six `<string>` panel renders cost 61.2ms, of which the ~41
queries at line 116 account for only ~24ms. The other ~37ms is template interpretation plus a
`RequestContext` built per panel, which re-runs every context processor — including
`_build_nav_menu`. That last part is measurable and untested.

### Blocked: the dataset has no VirtualMachines, Clusters or VirtualDeviceContexts

Two candidates below cannot be priced at all against the current dataset, and adding workload
scenarios for them would measure empty tables:

    VirtualMachine          0
    Cluster                 0
    VirtualDeviceContext    0
    JobResult               2
    ObjectChange       36,552

So finding 53's property-column blind spot (`VirtualMachineUIViewSet` and
`VirtualDeviceContextTable` both declare the `primary_ip` property column) and finding 54's two
remaining per-cell-compiling columns (`JobResultColumn` needs more than 2 rows to show anything)
are all **unmeasurable until the dataset generator creates these objects**. That is dataset work,
not experiment work, and it is the prerequisite for three separate queue entries — which makes
it better value than any of them individually.

`ObjectChange` at 36,552 rows is why the changelog entry above *is* measurable.

### The device list's two remaining queries cost 54.5ms

Finding 53 replaced 100 point lookups (~99ms) with two queries, and those two cost
**54.5ms** of `db_in_render` on `ui.device.list.rows` — about 27ms each. The trade was
plainly worth it, but 27ms for a single query over a 100-row page is worth understanding
before anyone assumes the database side of that page is now finished. Related: `api.device.list`
spends **436.3ms across 8 queries** (~55ms each) and has been queued separately for longer.
Same order of magnitude per query, two different endpoints — which suggests the cost belongs
to the device queryset itself rather than to either view.

### A property-backed table column is invisible to the select_related the table derives

Found by finding 53 and left deliberately unfixed there, because it is the general form of
what that finding patched in one place.

`BaseTable` builds a queryset's `select_related` from its visible columns by walking each
accessor through `model._meta.get_field()` (`nautobot/core/tables.py:277`). A property is
not a field, so `get_field()` raises `FieldDoesNotExist`, the walk breaks, and the column
contributes nothing — silently. Every FK the property reads is then a point lookup per row.

Four known sites, one measured:

| site | column | state |
|---|---|---|
| `DeviceUIViewSet` | `primary_ip` | fixed by hand, finding 53 |
| `VirtualMachineUIViewSet` | `primary_ip` | same shape, `select_related("tenant__tenant_group")` only |
| `VirtualDeviceContextTable` | `primary_ip` | same shape |
| any future property column | — | fails the same way, with no warning |

Neither VM nor VirtualDeviceContext is in the workload, so their cost is inferred from
shape rather than measured — **adding those scenarios is the cheap first step**, and it
prices the general fix before anyone writes it.

The fix worth pricing is an explicit hint the derivation honours when the accessor is not a
field — the column declaring which real FKs it reads. That fixes all four at once and stops
the next property column reintroducing the N+1. It also touches machinery every list view
in Nautobot uses, so it needs the full suite and a control set well outside dcim, which is
why it is a separate experiment rather than part of finding 53.

*Not established:* whether the device list view `select_related`s `primary_ip4`/`primary_ip6`.
The 137.1ms accessor figure sits close to the no-select_related measurement of 153.8ms,
which suggests it does not — but the check errored and was not repeated.

### Fragment caching with a derived change stamp

**Conditional on the item above.** Only worth building if the fixed cost turns
out to live in fragments that are worth caching.

The conceptual move is to stop treating a cache as "correct or wrong" and treat
it as a staleness budget with a stated number. Three tiers, increasing in price:
scope-bounded (the window is one request, so staleness is zero by construction —
where 13 of the 22 accepted changes sit, and why none of them needed a staleness
argument), time-bounded (the window is a TTL, and someone has to own the number),
and deploy-bounded (the window closes when the code changes, so it is free for
anything that does not vary at runtime).

**What makes it sound where finding 17 was not.** That one died because
*enumerated* invalidation has holes: `QuerySet.update()` and `.bulk_update()`
compile to one statement and emit no `post_save`, so a receiver-based scheme has
an unbounded window through a path nobody remembers to cover. A single "did
anything permission-relevant change?" stamp needs to enumerate nothing.

**Derive the stamp, do not maintain it.** A stamp bumped by a signal receiver
inherits exactly the hole it was meant to close — worse than no stamp, because
now a stale cache is accompanied by an assertion of freshness. A derived stamp is
computed from the data, so no write path can bypass it: `MAX(last_updated)` over
the permission tables is the cheap version and still misses a raw `.update()`
that does not touch the column; the database's own per-table modification
counters cannot be bypassed by any ORM path at all.

**Two-level it so the check is nearly free.** Stamp in Redis behind a very short
TTL, derived from the database when that expires. The expensive check runs at
most once per second per process, every other request pays one Redis read, and
the staleness window becomes the stamp's TTL rather than the fragment's.

**Where the budget can be spent, and where it cannot.** Cache what gates
*display*; never cache what gates *action*. A stale nav item or column set is
wrong for N seconds and the consequence is bounded by what the fragment contains,
which is enumerable. A stale authorisation decision that gates a POST is not
bounded by N at all, because the damage is a step function over the actions
reachable in the window rather than a function of its length. That is what
finding 19 was reaching for — both windows were bounded; the consequences were
not comparable. Conveniently the split falls the right way: the expensive half of
a page is display, and action-gating checks are single permission evaluations
rather than registry walks.

**What would kill it.** If the fixed cost turns out to be the nav menu after all,
finding 12 already took that win with a request-scoped memo needing no
invalidation, and finding 19 measured what cross-request caching adds on top at
**~0.6ms** — which fails the stopping rule in this queue. The idea then has no
prize to collect, whatever its risk profile.

### Nested transactions on the write path, and whether any of them earn their SQL

A single REST cable create with one termination issues **6 SAVEPOINT/RELEASE pairs**
— 12 statements of ~125, measured by diffing the SQL of the two arms of queue item 5's
A/B on the one-ended control. They come from functions that each open
`transaction.atomic()` without knowing whether a caller already did: DRF's
`perform_create`, `Cable.save()`, `defer_cable_path_rebuilds()`, `rebuild_paths()`,
and the row serializer's save. Defensive composition, locally correct everywhere,
redundant at runtime.

Each one is guarding something real, and the comments read as incident reports rather
than caution. `Cable.save()`: "otherwise we'd be left with an orphaned Cable row that
has no join rows and can't be cleaned up through the cable form." `rebuild_paths()`
deletes affected `CablePath` rows before rebuilding them, so a failure in between
removes connections with no error anywhere — the worst failure mode in the set.
`forms.py:4900`: "a creation failure mid-loop rolls back the delete." Cables are where
this concentrates because a cable create spans three tables and one of them,
`CablePath`, holds derived graph state that is expensive to recompute and invisible
when wrong. Creating an Interface is one row and wraps nothing.

**The distinction the code does not draw.** Wanting atomic *semantics* is right in all
five places. Wanting an *independently rollbackable* savepoint is only right if
something catches an exception from inside the block and continues issuing queries —
and `transaction.atomic(savepoint=False)` separates those: it keeps all-or-nothing,
enforced by whichever transaction is outermost, and emits no SQL when nested.

*Next step, and it is an audit rather than a change:* for each of the five, find
whether any caller catches and continues. The three `defer_cable_path_rebuilds()`
callers have been checked — none do, and `cables.py:1023` is nested inside
`Cable.save()`'s own atomic 100% of the time, so its savepoint is always redundant.
`Cable.save()` and `rebuild_paths()` are the unaudited ones, and they are reached from
bulk import, CSV, `loaddata` and the ORM as well as REST, so the blast radius is every
cable write path rather than one endpoint. A caller that catches and continues under
`savepoint=False` does not get a slow answer, it gets `TransactionManagementError` —
so this is a correctness audit whose prize happens to be performance.

**The narrow version was tried and rejected — finding 49.** `savepoint=False` on
`defer_cable_path_rebuilds()` alone: −4 queries, not the 2 first estimated, because the
helper is entered twice per REST create and both entries are already nested. Predicted
exactly, reproduced with zero variance, and rejected anyway on the trade —
`test_defer_rolls_back_on_exception` depends on the block being independently rollbackable
in order to verify its own guarantee, and 2% does not buy the right to narrow a documented
property whose failure mode is silent. Read that record before trying the wider version:
the same objection scales with it.

What the rejection did not touch is the shape of the problem. **61 `transaction.atomic()`
call sites in `nautobot/` outside tests, and zero use `savepoint=False`**, while one cable
create sits 6 savepoint pairs deep. `ATOMIC_REQUESTS` is unset, so these are real
boundaries rather than decoration: roughly 32 are view-level and would be subsumed by
`ATOMIC_REQUESTS = True`, and 22 are outside the request path entirely — `dcim/signals.py`
(5), `ipam/models.py` (3), `core/jobs` (3), `dcim/models/cables.py` (2), plus management
commands, the git datasource and the CLI. Jobs run in Celery workers, not requests.

*Two next steps, and the first is not a change.* Measure nesting **depth** across the write
surface rather than reasoning from one endpoint: `screen_writes.py` already covers 105
models, and reporting max savepoint depth per create would say whether 6-deep is a cable
peculiarity or the house style. If it is general the prize is ~2 statements × depth × every
write endpoint. Only then is it worth choosing between per-site `savepoint=False` — which
finding 49 shows needs each site's rollback dependency established first — and
`ATOMIC_REQUESTS = True` with the view-level atomics removed, which is correct-by-default
for HTTP but covers neither jobs nor model `save()`, and which commits partial writes when a
handler catches an error and returns 4xx unless `set_rollback(True)` is wired into DRF's
exception handler.

---

## Ranked below those, unchanged in substance

Items 3 to 6 are write-path work, ordered by the reasoning in *Why the write path sets the bar* further down. Items 7 to 9 follow it.

### 3. `full_clean()` re-validates every foreign key

**18 of 178 queries per cable created (10%)**, and 4 of 64 on
`ipam.ipaddresstointerface`. Django's `ForeignKey.validate()` issues one
`SELECT 1 … LIMIT 1` per FK, and `full_clean()` runs it across every FK on the
model — 27 distinct SQL shapes for one cable.

Nautobot opts into this twice, neither required by Django, whose `save()` never
validates: `validated_save()` (`core/models/__init__.py:198`) and
`BaseModelSerializer.validate()` (`core/api/serializers.py:769`). On the API path
the serializer has **already resolved every FK from the database** and then asks
the database whether those rows exist.

`full_clean(exclude=…)` and `clean_fields(exclude=…)` are supported Django call
signatures, so skipping re-validation of FKs the serializer just resolved is not a
hack. **Universal — every `validated_save()` in the product.**

*Next step, ~10 minutes:* split the 18 by phase — `clean_fields` against
`validate_unique` against `validate_constraints`. `perf/scripts/probe_full_clean.py`
already counts the phases; it needs to attribute queries to them.

### 4. Change-log serialization

**6% of a cable create, 15% of an `ipam.ipaddresstointerface` create.** Already
three findings deep (13, 22, 31), and it is the same API serializer the response
uses, at depth 1, per object.

*Next step:* establish whether depth is reducible for change logging specifically.
Findings 16 and 22 attacked what is *written*; nothing has attacked how deeply it
is *serialized*.

### 5. The REST create path does not defer cable path rebuilds

`defer_cable_path_rebuilds()` exists, is documented "for use when making multiple
CableToCableTermination table updates", and coalesces per-row signals into one
rebuild. `dcim/forms.py:4900` adopted it. `dcim/models/cables.py:1023` adopted it.
`CableSerializer._apply_terminations()` writes two rows in a loop without it.

Bounded to ~3% by the attribution above, but it is one line and it is the **fourth
instance** of the same shape on this branch (findings 7/11, 34, 36). *Cheapest
item on the list.*

### 6. `django-tree-queries` is already a dependency and `natural_key()` ignores it

Location is a `TreeModel`; the library answers "this node and its ancestors" with
one recursive CTE. `natural_key()` walks `val = getattr(val, lookup)` instead, one
lazy foreign-key load per hop. Finding 15 tried `select_related` and produced an
eight-way join that ran 62% slower — a CTE is a different shape: one query, one
table, ancestors as rows rather than columns.

No schema change, no invalidation, no staleness. **Unmeasured**, and the caveat
that matters is that a CTE is one query *per object* where the whole-table map is
one query *per table* — so it may beat the map on a single write and lose badly on
a page of 100.

*Next step:* probe it before believing it.

### 7. Affordance-adoption screen

**Kevin's reframing, and it is better than the one it replaced.** I had called
`Cable._get_termination_attr` a case of code diverging from its docstring. It
is not — the docstring says iterating `.all()` means a prefetched cache *is
honored if one exists*, and never claims one always exists. The method is
written correctly and deliberately to be prefetch-friendly, and finding 36 works
*because* its authors wrote it that way.

The real pattern is **an affordance added without its call sites updated to use
it**, and this branch has paid for it three times:

- finding 7 built a caching `TemplateColumn`; **finding 11** existed only
  because just the device tables had been switched over to it
- **finding 34** — `_with_connection_prefetches`, two endpoints left behind
- **finding 36** — `.all()` written for prefetching, never prefetched here

**Why it is worth building.** It is predictive rather than descriptive.
Screening endpoints by cost finds symptoms; screening affordances by adoption
finds causes, over a far smaller search space than 166 endpoints.

**First concrete candidate:** `TERMINATION_PARENT_FK_FIELDS`, whose own comment
says it exists "to extend `select_related` so that rendering
`termination.parent` ... stays query-free per row" — an affordance with a stated
purpose and an unaudited adoption list.

**Ordered after the row-rendering work only because writes are the unexplored axis, and that
work had a ranked list pointing at specific endpoints where this has none.** On
expected value per hour this may still beat it, and it is cheaper. Reasonable to
swap.

### 8. Finding 35 audit — undecided, needs a call

Finding 35 established that a container restart biases the in-process
measurement that follows it: bimodal ~99ms or ~165ms, set at process start and
held for a whole arm, so **three alternating rounds does not cancel it**.

**The debt.** Any in-process wall-clock figure on this branch taken shortly
after a restart is suspect. I do not know how many that is. Establishing it
means reconstructing the protocol used for each of 36 findings — archaeology,
not a grep.

**Honest cost:** roughly half a day to scope, potentially a day of
re-measurement after.

**What is not at risk:** query counts, which are load-independent and are most
of this branch's evidence. Conclusions should hold; some published percentages
may move.

**Decision needed:** pay it down, or note it in the report and defer. It is
currently noted in finding 35 and nowhere else.

### 9. Read-side leftovers — the ranking here is now unsupported

**Read "Screen the UI surface" before using this ordering.** This section was ranked below the
write path on the strength of finding 37, whose screen covers REST only, taken
against an inner loop that measured list-view shells. Finding 44 does not show
the ranking to be wrong; it shows it was never tested. Two of the sub-items
below landed today and are struck through rather than deleted, because the
reasoning that produced them is what "Screen the UI surface" inherits.

Kept as one item rather than four, because finding 37 established that none of
it competes with items 1–3. Ordered within itself by what it would teach.

- **DONE (today).** ~~Rank the screen on database time, not only on queries per object.~~ Both screens now emit a db-time ranking alongside queries per object. One correction to the figures below: `dcim.device` list is **432.0ms** over 8 queries, and it ranks **334th of 518** by queries per object rather than 100th — see finding 37's recorded correction. It is also not the largest read-side db-time item; `dcim.cabletocabletermination` at depth 1 costs 574ms. The investigation behind it is still open. Original text: `dcim.device`
  list is 8 queries, zero duplicates, and **438ms of database time** on a page of
  25 — 55ms per query, the largest read-side db-time item there is, reproducible
  across both runs (432 / 438 / 441ms). At 0.32 q/obj the ranking puts it 100th.
  Every read fix on this branch has been a query-count fix, and this endpoint has
  almost no queries to remove. `db_ms` is already recorded, so the ranking change
  is one line; the investigation behind it is not.
- **DONE (today).** ~~Fix the screen's coverage accounting.~~ `screen_reads.py` now reports attempted / exercised / full-page / measured separately. The figures in the original text conflated two of those: of 166 endpoints, **96 return at least one row, 49 return a full page of 10 or more, and 70 return none** — so quoting 49 as the exercised count understated by 47 exactly as quoting 166 overstated by 70. Original text: It measures 166 list endpoints and
  exercises 49. Seventy return zero rows against this dataset — `cluster`,
  `virtualmachine`, `vminterface`, `module` and the whole modules/templates
  family, `tag`, `service`, `rir` — and a zero-row endpoint reads as cheap when
  it is unmeasured. This is the failure "Screen the UI surface" is required to avoid, present in
  the instrument that raised the objection. Report attempted / exercised /
  measured separately and stop quoting 166.
- **The two residuals on tables large enough to compound.** `dcim.cable?depth=1`
  6.36 q/obj (159 queries, 145 duplicates, 3,278 rows) and
  `ipam.ipaddresstointerface?depth=1` 5.92 (148 queries, 130 duplicates, 2,937
  rows). Known shape, known fix, bounded payoff.
- **The small-table residuals, for completeness.** `vpn.vpntunnelendpoint?depth=1`
  9.76 q/obj (37 rows) · `circuits.circuit?depth=1` 7.32 (38) ·
  `dcim.interfaceredundancygroupassociation?depth=1` 6.60 (20) ·
  `vpn.vpntermination?depth=1` 6.52 (37). One page is the whole table for all
  four.
- **Finding 36 costs three fixed queries at depth 0** (list 5 → 8, detail 4 → 7)
  because its prefetches are unconditional and depth-0 serialization never reads
  them. It removed 472 at depth 1, so the trade is roughly 150:1 and the A/B
  never saw it because it measured depth=1 only. Conditioning the prefetch on
  requested depth is possible. Recorded rather than queued.

---

## Why the write path sets the bar — context for the ordering above

The branch has already taken **−33.7%** whole-workflow on the write path. What is
left is a long tail: a cable create touches **62 distinct call sites** through the
ORM and **86** through REST, and the top four together are **29%**. Nothing in
items 3 to 6 is a 40% fix, and they are ordered on that understanding.

**Stopping rule.** Anything under ~5% of the write surface gets a measurement, not
a day. The cost of ignoring this rule is on the record twice today: a natural-key
change that spot-checked at −7.8% measured **−0.4%** across the write surface
(finding 41), and cable-path computation — the mechanism everyone assumed was the
cost, including this queue — measured **3–5%** of a cable create.

### Closed by measurement — do not reopen without new evidence

| item | verdict |
|---|---|
| cable path recomputation | 3–5% of a cable create (finding 40 attribution) |
| natural-key map on 3 more models | −0.4% across the write surface (finding 41) |
| Redis-backed natural-key map | +0–2%, inside variance (finding 17) |
| eager `select_related` over the key chain | −666 queries, **+62% slower** (finding 15) |
| stored fragment / materialized path | blocked twice — see finding 41's note |

### Deferred by decision, not by measurement

The whole-table map returns `None` outside a `request_cache()` scope, so Jobs,
data migrations and nbshell pay the full hop-by-hop walk that HTTP callers stopped
paying at finding 14. Extending the scope to `web_request_context()` was built and
measured (it took an ORM cable create 178 → 166) and then **withdrawn**: a Job
holds that scope for minutes rather than milliseconds, which turns the map's
accepted staleness window into a real one, and `QuerySet.update()` emits no signal
that could invalidate it. Revisit only with an invalidation story that survives
`update()`.

---

## Done: the read screening pass was re-run (finding 37)

518 measurements over 166 list endpoints in 82s against `4a925fb43`, same
instrument and same dataset as the first run, so the two compare directly.

**The answer to the question it was asked: nothing on the read side outranks the
write path.** Ten of 518 measurements moved and 508 are byte-identical, so the
four fixes are surgical and the residual list is the old list with its top two
removed. The four highest per-object costs sit on tables of 20–38 rows. Only two
residuals sit on tables large enough to compound, both the same per-row N+1
shape fixed four times already. **The order below stands.** What is left on the
read side is collected as item 5.

Two things the run found that the ranking method cannot see are in that item as
well, and they are worth more than the residual list.

## Done: the write screening matrix is built (finding 38)

`perf/scripts/screen_writes.py` + `perf/scripts/payloads.py`. 152 of 166 API list endpoints
accept POST; 105 measured across `create.x1`, `create.x10` and `update.x1`, 257
measurements in 211s. All three requirements below were met, and the third one
turned out to matter for a reason nobody predicted.

**Schema-valid payloads per model — built, from `serializer.fields` rather than
from databot.** That is the same source DRF's OPTIONS metadata is built from,
read directly so the field *objects* are available: a related field's `queryset`
and the model field's `limit_choices_to` are what make it possible to pick a
value that validates. Three non-obvious facts were the difference between 70%
coverage and 40%, and they are in finding 38 and the README.

**Coverage accounting — built, and it reports its own exceptions.** 152 attempted
/ 130 built / 105 accepted / 105 measured, of which **82 from field metadata
alone and 23 needing a hand-maintained seed entry**. Both halves print every run,
so "we measured 105 models" can never mean "we measured 82 and hand-fed 23".

**The warmup convention — needed, for a different reason than finding 33 gave.**
Finding 33's warmup is about PostgreSQL's shared buffers after a clone. This
screen does not clone; it rolls back. It needed a warmup anyway, because **a
rolled-back transaction restores the database and not the process**: the
natural-key and tag caches are cold on the first pass only and survive the
rollback. 47 of 252 measurements had unstable query counts without it, and 1 of
257 with it.

**What it found, which is what made the row-rendering work more interesting rather than less.**
The median marginal cost of creating one more object is **12 queries**; the
median read costs **0.28 queries per returned object**. The cheapest create on
the whole surface (3.0) is more expensive per object than 47 of the 49 read
endpoints that return a full page. Top of the ranking: `ipam.ipaddresstointerface`
65.6 q/obj, `dcim.interfaceredundancygroupassociation` 46.2, `dcim.cable` 44.9
(one SELECT repeated 280 times per 10 cables), `dcim.device` 40.0,
`ipam.prefix` 30.0, `dcim.interface` 23.8.

**Byproduct, and it is a correctness bug not a performance one.** POST to
`vpn.vpnprofilephase1policyassignment` or `...phase2...` returns HTTP 500
unconditionally — both models are plain `BaseModel` with no custom-field support
and both serializers are `NautobotModelSerializer`, which passes
`_custom_field_data` to the model constructor. Those two endpoints cannot be
written to at all. Out of scope here; worth reporting upstream.

---

## Done: the −37% is real, and it is diffuse (findings 39, 40)

Reproduced as a controlled A/B on the measurement host — same box, same dataset
(datacenter/large), arms alternated, trees proved different by content hash
before each run. **−33.7% wall clock** (1304s against 1967s), −15.0% queries,
−9.7% server execution time, with neither arm hitting the client-side cable
timeout. The earlier pair, taken before that client fix, gave −31.3% (1450s
against 2112.5s median) with 0.2% within-arm spread.

**Of the 662 seconds saved, 2.9 are database execution — 0.44%.** The write-path
win is Python, not SQL, which is what findings 13, 4 and 31 actually are. Every
instrument on this branch gates on query count, and query count rates this work
at −13% when it is worth −31%.

**The queue's proposed method would have given the wrong shape.** It said to
revert finding 13 alone and re-time. Running the write screen against both arms
costs the same machine time and gives the per-model breakdown: 153 of 260
measurements improved, **zero worse**, across 40 models, top three 75% of the
saving. There is no finding-13-shaped thing to hunt for — nothing dominates.

**A retracted figure, kept visible.** This item briefly carried an "adjusted
−40.5%", produced by subtracting 16 × 30s of supposedly-discarded work from both
arms. The work was not discarded — the server committed it and the client
confirmed rather than re-created it — and the measured cost of the client fix was
146s, not 480s. Re-running against the fixed client gave −33.7%. Where a re-run
is affordable, re-run rather than adjust.

---

---

## Standing context

**Finding 22 was re-measured on 2026-09-06, declined on speed, then taken on
storage.** Its recorded −14.2% had halved to −7.7% on the ORM path, because
finding 31's tag-cache fix removed one of the two queries per record it was going
to save; on the REST path, weighted by the datacenter dataset, it is −2.1%
queries and −0.26% wall. So the write-path case for it is gone. It landed anyway,
because `object_data` is **11.8% of the changelog table** and that table grows
without bound where `CHANGELOG_RETENTION` is long — a storage argument this branch
never thought to measure, and Kevin's call on customer evidence rather than mine.

**Two reusable lessons.** Re-measure a parked prize before implementing it, not
just before proposing it — this branch competed with itself and the ledger did not
notice. And a change declined on the axis you are chartered to measure is not the
same as a change that should not be made; say which axis the "no" is about.

**Still parked: finding 16**, and it should stay that way. It attacks the same
v1/v2 double-serialization as finding 22 but by writing `{}` into the NOT NULL
column, which makes the field present and lying — an empty dict is not "no data".
Finding 22 took the same win by the honest route and has landed, so 16 has nothing
left to offer.

**On carrying finding 22 on the clean-read branch.** The reason for keeping 22 off it was
that a changed API payload could break the databot apply producing the write-path
evidence. That evidence is now collected (findings 39, 40), and the change was measured
on both arms with `screen_writes.py` before landing — 102 models measured either side,
identical coverage — so the objection has expired. The branch in question was
`perf/verified`, since superseded by `perf/recommended` and preserved as the tag
`perf/archive-verified`.

**The largest measured gap is still environment, not code** — see the
observation of that name in `perf/report.md`. It should be re-measured now that both sides can
be taken at concurrency 1 under uwsgi.

**Process rules that cost time to learn** are in `perf/README.md` and the commit
messages. The two most recently earned: a process can look alive in `ps` and be
doing nothing, so check consumed CPU rather than elapsed time; and `invoke
tests --no-keepdb` blocks on a confirmation prompt unless `--no-input` is also
passed.

---

## Imported candidates: the perf series on Ken's fork

**Queued 2026-09-10.** A second performance series exists, independent of this branch:
38 product commits to evaluate — 8 probably already ours, 2 taking a different route to a goal
we share, 28 probably new — on `origin/vibed-api-ui-improvements` in
`~/repos/experiments/perf/ken-nautobot`, authored 2026-07-25/26, indexed by their own
`pr-breakout-manifest.md` at `43ae11c27` as 8 themed PRs. **None of it is upstream** —
checked against `upstream/next` (`29bdcaa57`) and `upstream/develop` (`ca72fa539`) on
2026-09-10: `display_prefetch_related`, `NATURAL_SLUG_ENABLED`,
`get_settings_or_config_memoized` and `replace_queryset` all return zero files in both. So
this is available work, not a rebase problem.

**Per-commit progress lives in [`perf/ken-import.md`](ken-import.md)** — one row for each of the
38, with a status (`open`, `assessed`, `ported`, `declined`, `superseded`) and the verdict that
produced it. Update the row in the same commit that records the finding; this section argues the
ordering, that file tracks the state.

**Each one gets assessed and accepted the same way as everything else here** — attribution
first, one experiment per commit, deterministic counter as the gate, wall clock as the
ranking, three alternating rounds, a control that cannot benefit, a findings record. A
commit arriving with someone else's number attached is a hypothesis, not a result. Their
manifest reports query deltas almost throughout and two wall-clock figures; on this branch
query count has ranked fixes wrong twice, so their numbers set expectations rather than
settle anything.

### Two facts that shape the work

**Cherry-picking will not be clean.** Their base is `25e55bb37` (develop, 2026-07-24); ours
is `c3605ae48` (next, 2026-09-11) — seven weeks and a different branch lineage. Expect to
re-implement against our tree and use their diff as the specification. The series was
assessed in full against the older `ce01a0464` baseline and closed at 38 of 38; this line
matters only if more of their work is imported.

**`nautobot/core/tables.py` is the collision hotspot.** Eight of the 38 touch it, and it
already carries findings 7, 11 and 54 (TemplateColumn compilation caching). Their
`UI | BaseTable` set rewrites accessor walking and column selection in the same file. Do
that subset last, or first and deliberately, but do not interleave it with anything else.

### Triage — needs verification, not to be trusted as written

**Probably already ours; assess for redundancy before spending a round on them.**

    6d2582b93  API|Caching|1   natural_key_field_lookups per model class    -> cf findings 2, 5
    08af89a00  API|Caching|2   natural_slug once per object                 -> cf findings 1, 4
    089ee666a  API|Caching|3   Location natural_key_field_lookups override  -> cf findings 14, 42
    56f4e299f  API|Caching|5   signal-invalidated config memo               -> cf finding 6
    fd584db6a  API|Caching|6   ProcessTTLCache for tree_queries reads       -> cf finding 6
    036404a90  API|Targeted|1  Device parent_bay reverse one-to-one         -> cf finding 25
    26222c5fd  API|Targeted|2  FrontPort/RearPort cable_peer prefetch       -> cf findings 26, 34, 36
    61e2fffef  Cabling|1       cable-peer/endpoint prefetch enrichment      -> cf findings 2, 9, 26

`API|Caching|5` and `6` are worth a real look rather than a dismissal: finding 6 made those
reads request-scoped, and theirs are signal-invalidated and TTL'd respectively. Different
lifetime, different risk tier, possibly a better answer than ours.

**Approach differs from ours where we share the goal — the most interesting pair.**

    2faf6adad  API|Generic|1   prefetch FK serializer fields at depth 0 *instead of JOINing*,
                               and prefetch nested serialization at all depths
    d14c9fe57  API|Generic|2   auto-prefetch GenericForeignKey model fields

Finding 2 extends the optimizer upstream already has, which `select_related`s FKs at depth 0.
Theirs replaces that JOIN with a prefetch, which is a different trade — more queries, smaller
result rows — and their manifest claims Device 266->57 ms and circuits depth=1
2,063->23 queries on it. If that holds on our dataset it may subsume several of our
per-viewset prefetches. Measure before assuming either way.

**`API|Generic|1` must be gated on wall clock, not query count, and this is the one place on
the branch where that is not a preference.** Measured 2026-09-10, `dcim-api:device-list` at
depth 0 runs **8 queries on stock and 8 on branch** — the upstream optimizer already covers
depth-0 FKs, so there is no N+1 left to remove and no query-count movement available. Their
266->57 ms comes from JOIN *width*: `select_related` widens every row with columns the
response barely reads. A query-count gate scores that change at exactly zero on the endpoint
they chose to headline. Measure row width (`response_bytes` is already recorded per
measurement) and wall clock, and state plainly in the finding that the deterministic counter
cannot see this one — the same shape as finding 51, where the gate had to become reversal
count because no query moved.

Their circuits figure is also the one place their ratio clearly beats ours: -98.9%
(2,063->23) against our -48.6% (356->183) on `circuits-api:circuit-list list.depth1`.
Different dataset and different base, so the absolutes do not travel — but a factor that
large lands directly on the unfixed vpn/circuits `depth1` cluster logged above, and is the
most valuable single thing in their series for us if it reproduces.

**Probably new to us.** 28 commits: `API|Caching|4` (custom_field_keys per serializer
field), `API|Behavioral|1-3` (URL route-shape memoization for hyperlinked fields and dynamic
form `data-url`s, plus a `NATURAL_SLUG_ENABLED` opt-out — note finding 51 memoized
reversals only in the nav menu, so these are disjoint sites), `API|Targeted|3`,
`UI|Views|1-2`, all eight `UI|BaseTable`, all ten `UI|Tables`, and `Cabling|2-4`.

Two of those land on open items above: `UI|Tables|1` batches the Prefix hierarchy lookups
(their figure: Prefixes tab 264->60), and `UI|Tables|3` replaces the Location tree-link walk
with `tree_depth` plus batched children (LocationType detail 155->59).

**Their column-level audit is the half this branch skipped**, and `perf/dataset-gaps.md`
says why: 66 of 166 endpoints return zero rows, so a per-column sweep on our dataset would
have been auditing empty tables. Theirs reports 183 tables and 1,757 columns down to 8 open
items, and 247 offenders in the port tables alone. Worth reading their audit output before
re-deriving it.

## Candidates found while assessing the imported series

**Queued 2026-09-12, all measured, none fixed. Promoted to "Next up" on 2026-09-13** rather
than listed again here, because a candidate described in two places drifts in one of them:

- the four `depth=1`/`depth=2` endpoints are **Next up item 1**, `?depth=2`
- the three per-row utilization aggregates are **Next up item 2**

Each would be its own experiment. Nothing else came out of that assessment unfixed.

## Done: the serial full suite is green, twice

**2026-09-13, on the rebased tree** (`perf/experiments` on `next` `c3605ae48`), which is
the run that matters now:

    Ran 17952 tests in 8289.986s
    OK (skipped=666, expected failures=1)

Zero `FAIL:` and zero `ERROR:` lines. The count rose by 419 because upstream added
tests. This is the gate on the rebase: the conflicts were resolved by hand in
`core/api/serializers.py`, `core/tables.py`, `core/views/utils.py` and two test
modules, and a hand-resolved tree can measure beautifully while being wrong.

**2026-09-12, against `perf/recommended` at `977eb44c6`** on the old baseline:

    Ran 17533 tests in 7626.657s
    OK (skipped=663, expected failures=1)

Also clean, and the collected count rose by 29 against the 17,504 on record at
`fd48eee32`. A fall in that count matters as much as a failure, because an import
error silently shrinks the suite.

    perf/scripts/arm_control.sh arm perf/recommended
    invoke tests --parallel-workers=1 -n -k --no-cache-test-fixtures

`git checkout perf/recommended` is the wrong way to arm for this. `perf/` and
`development/docker-compose.perf.yml` do not exist on that branch, so a whole-tree
checkout deletes the harness and the compose overlay the running container was
created with. `arm_control.sh` swaps `nautobot/` alone, and
`git diff perf/recommended -- nautobot/` came back empty afterwards.

Both branches hashed `6d0feb2102b90f4b` and their `nautobot/` trees differ in no file
of any type, so the run covers `perf/experiments` as well.

Serial is mandatory: the parallel runner dies during subsuite setup with
`MaybeEncodingError: cannot pickle '_thread.RLock'`, prints no test names, yields no
counts and exits 0. It reproduces at `cc45a35f5`, so it is not this branch's doing.

`--no-cache-test-fixtures` rules out the cached fixture as a source of stale state.
`--keepdb` stayed on and the log records `Using existing test database for alias
'default'`, so the test database itself was reused; `--no-keepdb` is still untried
and needs `--no-input` beside it or it blocks on a prompt. 2h11m wall, of which
7,627s was test execution.

## Done: re-baselined onto `upstream/next` `c3605ae48` (2026-09-13)

This was queued as "a check, not a rebase", on the reasoning that rebasing would leave the
cumulative table describing a baseline the tree no longer sits on. The rebase was done anyway,
and the reasoning turned out not to apply: **the whole table was re-measured on the new
baseline in the same session**, so nothing describes a tree that has moved.

**Upstream had moved 98 commits**, `ce01a0464` to `c3605ae48`, colliding with us on 12 files
rather than the nine predicted. Six conflicts across the 177-commit replay, and every one was
positional rather than semantic:

| where | cause |
|---|---|
| `.gitignore` | both sides appended; union |
| `core/views/utils.py`, ×3 | upstream inserted `get_overview()` immediately above `common_detail_view_context()`, which finding 03 rewrites |
| `core/tests/test_tables.py`, ×2 | both sides appended a test class at the same anchor; one import union |
| `core/tests/test_views.py` | import union |

**The test-file hazard this entry predicted did not materialise.** Both sides did add cases to
the same modules, but the additions were textually adjacent rather than overlapping, so git
stopped rather than merging something broken.

**The rebase is provably inert.** The net change to `nautobot/` is textually identical against
the new base — 63 files, `+3,022 −454`, 3,476 changed lines, zero differing — and the read loop
measures 884 queries pre-rebase and 884 rebased, with not one of the 57 scenarios differing.

**Upstream moved none of the instruments**, which is why figures either side of the re-baseline
are comparable at all. Stock at `c3605ae48` reproduced stock at `ce01a0464` exactly on every
deterministic counter: 3,597 read-loop queries, 15,035 read-screen queries, 22,578 write-screen
queries, identical to the integer, and within 0.2% on read-loop wall clock.

**No new upstream migrations**, so the dataset snapshot and both pristine templates survived
untouched. Dependencies moved only a `django-tables2` floor to `3.0.1`, which the image already
satisfied.

**Two traps upstream set for the harness, worth knowing before the next re-baseline:**

- **`tasks.py`'s default `python_ver` moved 3.13 to 3.14.** `perf/scripts/dc.sh` pins 3.13, so
  the harness path is consistent, but both resolve to the same compose project — an `invoke`
  command that recreates the container would swap the interpreter under a measurement while
  every hash and gate reported normally. Pin `PYTHON_VER=3.13` explicitly.
- **Upstream deleted 40 `tests/integration/*.py` modules**, migrating Selenium to Playwright.
  That is what exposed finding 80: arming stock → old-branch → stock left them behind and the
  same ref hashed two ways.

