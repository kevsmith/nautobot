# 18-hour plan: report restructure, demo branch, consistent methodology

Working checklist for the push to a team review. Ordered by dependency, not by
importance. Machine work is serial and host-bound; local work runs alongside it.

Deadline is self-imposed. If something slips, the cut list at the bottom says
what goes first.

## Where this stands

**The deliverable is in shape.** `perf/report.md` is 14KB in the target format,
all three cumulative rows carry real stock-versus-branch numbers, and
`perf/verify_report.py` passes 1,491 checks. `perf/recommended` is built and
independently verified. The full suite is green on the measured tree.

**Genuinely remaining, in order:**

1. ~~Verify the two new test modules pass~~ — **DONE: 11 tests, `OK`.** Five
   django-tables2 differential tests and six request-scoped cache scope tests,
   in areas that had **no coverage at all** before. Writing them found a
   coupling nobody had recorded: the caching `TemplateColumn` depends on
   `kwargs["bound_row"]` reaching `get_context_data()`, not merely on that hook
   existing — an assertion of `hasattr` would have passed while the code broke.
   There is now an explicit test for the kwarg contract. The
   `third-party-coupled` caveat is retired in `methodology.md`.
2. ~~`screen_writes.py --include-optional`~~ — **DONE, and the caveat could not
   be retired, only bounded** (finding 47). Across 73 models measured both ways
   the median understatement is **1.00×** — the floor *is* the cost for the
   typical model — mean 1.31×, max 4.96×. But **27 models could not be measured
   with populated payloads at all**, including `dcim.cable`, `dcim.interface`
   and `dcim.powerfeed`: a model with expensive optional relations is both the
   most understated and the hardest to build a payload for, which is the same
   cause twice. Caveat rewritten to say exactly that. Source data committed as
   `baselines/screen-writes-optional.json`.
3. ~~Whole-workflow apply pair~~ — **DONE.** stock 1,978 s / 846,932 queries /
   34,641 ms db against branch 1,245 s / 706,499 / 30,169: **−37.1% wall,
   −16.6% queries, −12.9% db** — better than the stale pair on every axis, and
   landing on the −37% Kevin originally observed. Row counts identical on both
   arms (536/1616/1648), so the runs did the same work. **Of the 733 seconds
   saved, 4.5 are database execution.** Campus dataset restored to exactly
   2902/8925/3278. **No stale measurement labels remain in the report.**
   Original note: Pre-flight verified
   `nautobot_pristine_campus` holds exactly `2902/8925/3278` before dropping
   anything, and the script restores the campus dataset from that template
   afterwards. ~60–70 min for both arms. Original note: It refreshes the last stale row
   (currently labelled as tree `a586fc5cf`), but `apply_arm.sh` calls
   `arm_control.sh reset`, which drops the database and re-clones from the empty
   template, destroying the 24,091-object read dataset. Reversible in 49s via
   `restore_snapshot.sh`, read baselines already committed — but destructive,
   and it is cut-list item 4.
4. **Commit** — the report's provenance line reads `tree af659dfd2 with 67 dirty
   path(s)`, which undercuts the reproducibility claim for anyone reading
   closely. Kevin's call.

**Last, after everything above is committed:**

5. **Move the harness scripts into `perf/scripts/`** so a reviewer can be told
   to ignore everything under `perf/` except `report.md` and `methodology.md`.
   Mechanical but wide — these all carry script paths and would need updating
   in the same commit:
   - **Container invocations**: `/source/perf/tier1_queries.py` and friends
     appear in `run_experiment.sh`, `run_screen_ab.sh`, `apply_arm.sh`,
     `measure.sh` and the README's worked examples.
   - **Script-to-script references**: `dc.sh`, `arm_control.sh`, `quiesce.sh`
     and `reset_db.sh` call each other by path.
   - **`README.md`** names most scripts in prose, several with line numbers.
   - **Findings `site` fields** name harness paths — `perf/screen_writes.py`,
     `perf/compare_screen.py`, `perf/tier1_queries.py`. These render into
     `methodology.md`, so they must move with the files or the paths go stale.
   - **`is_product_change()`** in `build_report.py` classifies a finding as
     harness-versus-product by whether every path in `site` starts with
     `perf/`. `perf/scripts/` keeps that prefix, so the classification survives
     — worth confirming rather than assuming.
   - **12 shell scripts derive the repo root as one level up** —
     `ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"` in
     `arm_control.sh`, `apply_arm.sh`, `dc.sh`, `measure.sh`, `quiesce.sh`,
     `reset_db.sh`, `restore_snapshot.sh`, `run_experiment.sh`,
     `run_screen_ab.sh`, `sync.sh`, `probe_clone_warmup.sh`,
     `record_expected_counts.sh`. Every one becomes `/../..` or they resolve to
     `perf/` and silently operate on the wrong tree.
   - **`build_report.py` and `verify_report.py` derive `PERF` and `ROOT` from
     `__file__`**, so both need one more `.parent`. `build_report.py` writes
     `report.md` from that path — get it wrong and it writes into
     `perf/scripts/`.
   - **13 findings name a `perf/` path in `site`, and two of them name data
     that must NOT move**: seq 44 and 46 reference `perf/workload.yml`. A blind
     `perf/X` → `perf/scripts/X` rewrite corrupts those. Rewrite only the exact
     filenames that actually move.
   - Guards that would catch a miss: `build_report.py --check`,
     `verify_report.py`, and `git grep -n 'perf/[a-z_]*\.\(py\|sh\)'` for
     anything left behind. None of them catches a wrong `ROOT` in a shell
     script, so those want reading by eye.

   **One thing to confirm first:** "all the scripts we've written" — everything
   executable under `perf/`, or only what was added in this session? The stated
   goal (tell reviewers to ignore `perf/` except the two markdown files) implies
   everything, since leaving half the scripts at top level does not achieve it.

   Data directories stay where they are — `findings/`, `baselines/`,
   `results/`, `workload.yml`, the snapshots. Only executable harness code
   moves.

**Dropped, with reason:**

- **Read screen A/B.** Superseded. It covers the REST surface only, and the
  57-scenario read loop now answers the same question *with wall clock* and
  includes the UI surface the screen never touched.
- **A `/api/dcim/locations/<id>/stats/` scenario.** Real, and 92ms against a
  457ms document — too small to earn a slot before the deadline. Recorded in
  the queue instead.
- **Silk removal** and the **UI byte-stability normalizer** — cut-list items 1
  and 2, unchanged.

---

## State of the world, as established

| Fact | Value |
|---|---|
| `next` | `ce01a0464`, identical to `upstream/next` |
| `perf/verified` | 23 commits, **linear on `next`**, zero merges, 44 files +1,453 −283 |
| `origin/perf/verified` | published at `f0d690a61`; local is one commit ahead at `02d04700f` |
| `perf/experiments` | same `nautobot/` tree as `perf/verified`, plus harness and records |
| Thin commit messages on `perf/verified` | **20 of 23** carry only "Application-code portion of `<sha>` from perf/experiments" |
| Non-upstream files on `perf/verified` | `development/docker-compose.perf.yml`, `nautobot_config.py`, `uwsgi-perf.ini` |
| Last full suite | 17,504 tests, 2h18m, green at `fd48eee32`. Findings 22, 37–42 landed after it |
| `report.md` today | 46 KB, 636 lines |
| Findings missing a `commit` | 23, 24, 27, 34, 36, 41, 42 |
| Product tree content hash | `90dbd96ffbe…` — every Phase 0 and Phase 3 result attaches to this |
| `report.md` after Phase 4 | 13 KB, ~140 lines, tables only |
| Finding 22's recorded commit | was `9b8831711`, **a reverted commit**; corrected to `3c683a405` |
| Commit audit | all 32 recorded commits resolve, are ancestors of `perf/experiments`, none reverted |

**The scheduling unlock:** the measurement protocol verifies tree *content
hashes*, not commit SHAs. Rewriting commit messages does not change content, so
the test suite and the A/B runs can start now against the current product tree
and their results transfer to the rebuilt branch unchanged.

## Phase 0 — start the long-running machine work (T+0:00)

- [x] Record the current product tree's content hash (`nautobot/**/*.py`) so
      every result below can be attached to it
- [x] Kick off the full suite on that tree: `invoke tests --no-parallel
      --no-keepdb --no-input` — **2h18m**
- [x] Confirm nothing else is queued on the measurement host; the suite and any
      timing run cannot overlap

## Phase 1 — instrument work (local, parallel with Phase 0)

### The list-view measurement defect (found mid-session, highest priority)

`nautobot/core/views/renderers.py:96` builds a list view's table over
`queryset.none()` unless the request carries `HX-Request`. The browser supplies
it on a follow-up request fired by `hx-trigger="load"` in
`generic/object_list.html:152`. The harness never sent that header — zero
occurrences across `workload.yml`, `workload.py`, `tier1_queries.py` and
`bench_endpoints.py` — so **all 20 `ui.*.list` scenarios measured a page that
rendered no table rows.** `ui.interface.list` asks for `per_page: 100` and
renders none of them.

Consequences, all now corrected or recorded:

- "List views were already efficient at 9-13 queries" is false. They are 9-13
  queries because they render nothing.
- Finding 07's control ("ui.device.list and ui.interface.list both within
  noise") and finding 12's ("ui.interface.list flat") measured empty tables and
  prove nothing about list views.
- Finding 11 converted 67 `TemplateColumn` call sites across ipam, circuits and
  dcim tables; all of that work happens in the unmeasured request, and it is
  the one finding with no wall-clock number at all.
- Kevin's dev-tools observation that list views are faster is the correct one.

- [x] `measure()` in `tier1_queries.py` takes `headers`; `workload.py` carries
      them from the YAML; the tier1 loop passes them and records them on the
      result — **1 pt**
- [x] 18 `ui.*.list.rows` twin scenarios generated from the existing list
      scenarios, each carrying `HX-Request: true`. Both halves kept, since a
      page load pays for both. Workload goes 39 → 57 scenarios — **1 pt**
- [x] `render_cumulative` sums the intersection of the two arms rather than
      each file whole, which would otherwise have compared 57 scenarios against
      38 and rendered the difference as a regression — **1 pt**
- [x] Page anatomy established in Firefox (finding 44). **List views are two
      requests to the same URL**: document renders the shell, an htmx XHR
      renders the rows. Device list 471 ms + 692 ms; interface list 453 ms +
      **1,195 ms**, so 72% of that page's server time was unmeasured.
      **Detail pages are one request** — device detail 827 ms with no
      follow-up — so their 29–52% query reductions are the whole page and need
      no correction. Location detail adds one small `/stats/` API fetch
      (92 ms), also unmeasured. Timings inflated by the concurrent suite;
      the anatomy is the result, not the absolutes
- [x] **Confirmed.** All 18 twins return 200 and render rows. `ui.prefix.list`
      is **310 queries with 297 duplicates** against its shell's 9, and does not
      scale with page size (310 at default, 311 at per_page=100) — a fixed
      whole-tree cost, not the row-scaling N+1 the queue assumed.
      `ui.device.list` *is* row-scaling: 77 duplicates at 50 rows, 77 at 100.
      Cables and IP addresses are clean at 8 queries and zero duplicates, so
      their cost is pure per-row Python
- [x] **Chrome baseline scenario added** (`ui.chrome.404`). A 404 renders the
      full chrome — `404.html` → `40x.html` → `base.html` — with a static card
      where content goes, so it measures what every page costs before it
      renders anything of its own. Needed `expected_status` support in
      `workload.py`, `tier1_queries.py`, `tier2_latency.py` and `compare.py`
      (Tier 2 refused to time anything non-200; `compare.py` treated a
      non-200 baseline as broken), plus a documented `path` opt-out from the
      reverse()-only rule, since a 404 has no view to reverse. Kevin found the
      control by mistyping a URL — **58 scenarios now**
- [x] **Chrome contradiction RESOLVED, and the committed numbers were right.**
      On a gated box `ui.chrome.404` is **82ms** — within a millisecond of
      `ui.search` at 81ms, which renders identical chrome. Zero-row list
      documents run 151–256ms, so per-model list machinery is **68–174ms** on
      top of chrome, a 2.5× spread: extras models cheap (objectchange, role,
      status at 68–70ms), dcim and ipam dear (location 174, prefix 154). **The
      per-model term dominates.** Kevin's 274ms 404 was inflated 3.3× by the
      suite competing for the same cpuset — a figure taken outside the quiesce
      gate cannot arbitrate between two taken inside it
- [x] **tier2 A/B done** — see finding 46. List-view rows improve on 18 of 18,
      median −18.6%, with **zero query change on any of them**; the document
      half is unchanged at −1.3%. Aggregate 33,606 → 22,892 ms (−31.9%).
      Committed as `baselines/tier2-57-{stock,branch}.json` and wired into
      `cumulative.json`, so the read row now carries wall clock
- [ ] Consider a scenario for `/api/dcim/locations/<id>/stats/` — a real detail
      page requests it and no instrument covers it
- [x] **DONE** (done as `tier1-57-stock.json`). Original: Re-baseline the stock arm with all 57 scenarios. `large-tier1-baseline.json`
      has 38, so the readloop aggregate must come from the Phase 3 stock run
      rather than from it


Prerequisite for the read-screen A/B. `screen_reads.py` currently takes **one**
measured request per endpoint, which is fine for deterministic query counts and
useless for a wall-clock comparison.

- [x] `screen_reads.py`: `--reps` (default 3), one discarded warmup, median
      wall/db, `query_count_stable` flag, modelled on `screen_writes.py:234
      repeated()`. Cost: a run goes from 83.5 s to roughly 3–4 min — **2 pts**
- [x] `screen_reads.py`: attempted / exercised / measured accounting. Verified
      against the committed run: of 166 endpoints, **96 return at least one
      row, 49 return a full page of 10 or more, 70 return none**. Finding 37's
      "exercises 49" conflated full-page with exercised — 49 understates by 47
      exactly as 166 overstates by 70. All four numbers now reported
      separately, and finding 37 carries the correction — **1 pt**
- [x] `perf/compare_screen.py`: diff two screen runs by measurement id, emit
      matched coverage plus per-model and aggregate deltas as JSON the report
      can read. Findings 39/41/42 each did this by hand — **2 pts**
- [x] Both screens: rank on db time alongside queries per object. `db_ms` is
      already recorded; summary code only. Read side **done**; write side
      pending. `dcim.device` list is 432 ms over 8 non-duplicate queries and
      ranks 334th of 518 by queries per object — **1 pt**

## Phase 2 — `perf/recommended`, the upstream-facing branch (local, parallel with Phase 0)

A new branch rather than a rewrite of `perf/verified`, for three reasons.
`origin/perf/verified` is published, so rewriting it means a force-push that
invalidates SHAs already recorded in findings and in commit messages on
`perf/experiments`. Finding 39's record names `perf/verified` as a measurement
arm ("stock next 1,967 s, perf/verified 1,304 s"), so that name has to keep
meaning what it meant when the number was taken. And verification reduces to
one diff between the two branches.

Not a from-scratch cherry-pick either: `perf/verified` is already linear on
`upstream/next`, so clean application is proved by construction. What is
missing is self-contained messages and the absence of harness files.

`arm_control.sh` takes the ref as an argument rather than hardcoding a branch,
so Phase 3 needs no change either way.

- [x] Backfill the 7 missing `commit` fields. Known: 34 → `2100781f0`,
      36 → `e29a09f28`, 42 → `af659dfd2`. For 23, 24, 27 and 41 the change was
      never implemented — leave those empty, which is itself informative — **1 pt**
- [x] **DONE** (built; `perf/recommended` at `85fbb16c4`). Original: `perf/build_branch.py`: rebuild the branch commit by commit with
      `cherry-pick -n`, excluding `development/` and `perf/`, committing each
      with a message generated from the finding's `summary` + `basis` +
      `result` + `caveat`. Same source as the report, so the two cannot
      diverge — **3 pts**
- [x] **DONE** (6 subjects rewritten, all now <=70 cols, `perf:` prefix dropped). Original: Rewrite the three lab-note titles into change descriptions ("…and find
      why a control read +73%", "…and falsify what finding 30 blamed")
- [x] **DONE** (all 22 carry `recommended_commit`, one added line per file). Original: Write the `perf/recommended` SHA mapping back into each finding, so the
      report's Commit column resolves on the branch the team is shown rather
      than on `perf/experiments` — **1 pt**
- [x] **DONE** (verified independently: only the 3 development/ paths, nautobot/ byte-identical, next is an ancestor, 22 commits, 0 merges). Original: Verify: `git diff perf/verified perf/recommended` shows only the three
      `development/` files, and `git merge-base --is-ancestor upstream/next
      perf/recommended` passes
- [x] **DONE** (untouched). Original: Leave `perf/verified` untouched. It is the measured arm of record and the
      before/after for the branch rebuild

## Phase 3 — measurement runs (machine, serial, starts when the suite finishes)

The workload grew from 38 to 58 scenarios today (18 `ui.*.list.rows` twins plus
`ui.chrome.404`), so this is larger than first planned and the order matters.
Every arm: alternated, trees proved different by content hash, container
restarted between arms, loadavg below 0.7 before starting.

- [x] **Step 0 done, and it caught a harness bug** — see finding 45. Validate: One tier1
      run on the current tree with `--only rows`, plus `--only chrome`. Checks
      that the 18 twins return 200 and render rows rather than shells, and that
      `ui.chrome.404` returns 404 and is not flagged. Minutes. **If the twins
      come back at the same query count as their shell twins, the header is not
      reaching the view and everything below is worthless** — so this gate
      first.
- [x] **tier1, both arms, done.** stock 3,166 → branch 1,824 (−42.4%) over 57
      scenarios, zero implausible records, committed as
      `baselines/tier1-57-{stock,branch}.json` and wired into
      `cumulative.json`. **0 of 18 row-rendering scenarios changed**, which is
      finding 44 confirmed from the other direction. Original item: Query counts are deterministic and
      fast, so this is the cheapest way to get the list-view row answer. Also
      re-baselines stock at 58 scenarios, which the readloop aggregate now needs
      — `large-tier1-baseline.json` has 38 and `render_cumulative` compares the
      intersection, so without this the new scenarios are silently excluded from
      the total.
- [~] **tier2, both arms — RUNNING** (gated swaps, n=30, c=1, ~90 min).
      Original item: The wall-clock answer, and the only
      instrument covering UI list views. ~45–60 min for two arms x 3 rounds.
      Answers "are list views faster" and, with `ui.chrome.404`, settles the
      chrome-versus-per-model contradiction in the same run.
- [ ] **Read screen, both arms.** REST surface, per-object ranking, ~20 min.
      Covers the REST surface only — `screen_reads.py` filters for `-api`
      namespaces — so it does not answer the UI question above.
- [ ] **Write screen, both arms.** Supersedes finding 39's pair, taken at
      `a586fc5cf` before findings 22, 41 and 42; ~40 min.
- [ ] **Whole-workflow apply pair.** ~60 min, and **cut-list item 4** — if the
      day compresses, the two screen aggregates plus an honest note about the
      apply figure's tree is a fine place to land.
- [x] **All comparison JSONs committed and wired.** `tier1-57-{stock,branch}`,
      `tier2-57-{stock,branch}`, `screen-writes-ab`. `cumulative.json` reads
      them; the provenance line names them. Only the apply row is still stale,
      and it says so.
- [x] **Aggregates now report their distribution, not just their sum.** A
      summed total invites "everything is 32% faster", which the first person
      to load a cheap page falsifies. Each row now states how many measurements
      moved, the median move, and how much of the saving is one item — the read
      total is **46% one endpoint** (`api.interface.depth1`) with a median move
      of −8.3%. Per-measurement wall figures below 100ms are excluded and the
      count said, per finding 43: without that filter the write row advertised
      "15 up to +46.7%" worse, which is noise. With it, **132 of 132 improved,
      median −16.0%, none worse**.

## Phase 4 — report restructure (local, wires up after Phase 3)

Target format: opportunities, cumulative effects, tier table, accepted table,
rejected table, methodology in 3–5 sentences, three one-sentence observations.
No per-finding entries, no caveats section.

- [x] Three cumulative renderers reading the Phase 3 JSONs. Replaces
      `render_cumulative()`, which is hardwired to `tier1` query counts over 38
      scenarios — **3 pts**
- [x] Tables-only mode: stop emitting per-finding entries into `report.md`, and
      retarget every `Change` link from `#anchor` to `methodology.md#anchor`.
      `render_entry()` becomes methodology-only — **2 pts**
- [x] Tier table as its own section, ordered per the target format — **1 pt**
- [x] Commit column on the **accepted** table only, pointing at
      `perf/recommended`, with a line naming remote and branch. Not on the
      rejected table: a rejected change is not on that branch at all, and
      several rejected findings share the commit that recorded the decision
      rather than one that implemented anything — **1 pt**
- [x] Observations cut to three, one sentence each: query count ranked the
      fixes wrong; the database is not the bottleneck; five findings trace to an
      affordance whose call sites were never updated
- [x] Purge every round-one absolute figure from `report.md`; all report numbers
      come from the current host — retires the cross-machine caveat — **1 pt**

## Found during Phase 3, recorded as findings

- **45 — query capture silently returned zero once the connection's query log
  saturated.** `CaptureQueriesContext` slices `len(connection.queries_log)`,
  a `deque(maxlen=9000)` shared per process. The stock arm's last six scenarios
  reported 0 queries on status-200 responses with full bodies and
  `query_count_stable: True`. Worse, the failure is **graduated**:
  `api.interface.depth1` reported 495 against a true 1,067 before the log
  saturated fully. 19.5% of the stock arm's queries went uncounted, and it
  biases the *baseline* arm, which is the worst possible direction. Fixed three
  ways — clear the log per capture, raise the limit to 18,000, and refuse a
  200-with-body-and-zero-queries record as an instrument failure. **All twelve
  committed baselines scanned and clean**, so no published number was affected.
- **A container-state hazard I nearly published.** After the tier1 arms I
  restored the tree with a plain checkout, no restart. Disk was branch; uwsgi
  still had stock loaded. A tier2 probe at that moment measured stock while
  every label said branch. `arm_control.sh` exists to prevent exactly this and
  I had bypassed it for tier1, legitimately, then almost carried the bypass
  into an instrument where it matters. Tier 2 now goes through `arm_control.sh`
  for every arm.

## Phase 5 — retire the caveats (local, uses the slack)

The report ends with no caveats section. Each of these is what makes one
obsolete rather than restating it.

- [x] **DONE** (placed in the methodology paragraph and the methodology.md caveat instead of the fact bar, which now carries no figures at all). Original: Full suite result from Phase 0 becomes a fact-bar row — retires the
      partial-coverage caveat
- [ ] django-tables2 guard test: assert the reimplemented hooks still exist and
      that cached and uncached rendering produce identical output, so a version
      bump fails in CI instead of drifting silently. The one caveat that is a
      correctness risk rather than a measurement limit — **2 pts**
- [ ] Request-scoped cache tests: each cache empty at request start, responses
      byte-identical cached vs uncached. Findings 30/34/36 already do the
      sha256 comparison; this generalizes it — **3 pts**
- [ ] `screen_writes.py --include-optional` run, reporting floor and populated
      side by side — retires the "measures a floor, not a cost" caveat
- [x] **DONE** (gone; the fact bar row says it). Original: Delete the "accepted on this branch, not upstream" caveat outright. The
      fact bar row already says it

## Queued, not for today

- **Rename the instruments for what they measure.** "Tier 1/1W/2" collides with
  the risk tiers `A`/`B1`/`B2`/`C`, and the numbering implies a hierarchy that
  stopped being true — Tier 2 at concurrency 1 holds a 1.6% median spread and
  agrees with in-process to 1.08×, so it is a peer rather than a confirmation
  step. Blocked on blast radius: dozens of finding records cite the script and
  baseline filenames as historical fact. `methodology.md` now carries a
  disambiguation table as the interim fix.
- **Replace cassowary with a ~40-line urllib driver.** Its own docstring lists
  three workarounds it forces: aggregate-only metrics needing one invocation per
  endpoint, header values split on commas, and no per-URL column.
- **A scripted browser anatomy check, as a harness component.** Not a timing
  instrument — an assertion that the request set still matches what the workload
  claims (a list view is document + one htmx XHR; a detail page is one request).
  Today's pass is a snapshot of one release; when a deferred panel appears in a
  future version the harness goes stale silently, exactly as it just did. This
  is the only instrument that would have caught finding 44.
- **A dedicated browser client box, wired to the same switch as hannah.**
  Kevin has a spare NUC. This is the better shape than headless Firefox on
  hannah, and it retires that plan's blocker: Firefox no longer competes for
  cpus 3,7 and no longer trips `arm_control.sh`'s 0.7 loadavg gate. Wired GigE
  puts ~0.2-0.4ms in the path against WiFi's 3.6-8.3ms at 1.96ms stddev, which
  is under 0.2% of a 300ms page and inside the harness's existing 1.6% spread.
  It is also closer to production than client-on-server would have been.
  - **The unlock is reproducibility, not precision.** With a dedicated client
    there is somewhere to run a *committed* browser driver (Marionette or
    Playwright under `perf/`) with reps, medians and alternated arms. That is
    what makes browser measurement an instrument rather than an interactive
    session. Keep the MCP server for exploration, which is what it was good at.
  - Measures what nothing currently does: render time, the list-view
    document-then-XHR sequence as a sequence rather than two isolated requests
    summed, and continuous validation that the workload's URL list still
    matches what the app requests. Finding 44 is the cost of not having that.
  - Install Firefox from Mozilla's tarball, pinned. Ubuntu's `firefox` is a
    snap transitional package and a client that auto-updates silently changes
    the instrument. Enable the `network` tool module if using the MCP server —
    the default `basic` preset omits it.
  - **The spare NUC is a BOXNUC8i5BEH — an i5-8259U, identical to hannah**
    (confirmed: hannah reports i5-8259U, `no_turbo=1`, governor `performance`,
    pinned at 2,300,000 kHz). Two consequences. The report's "absolute wall
    clock is not comparable across machines" caveat does not apply between
    these two, so the client box can also independently reproduce a server
    measurement as a cross-check. And **match hannah's clock policy: turbo off,
    governor performance.** With turbo enabled the client drifts *within* an
    arm as the package heats, which is finding 35's failure mode pointed at the
    client; pinning to base clock removes both turbo and frequency scaling as
    variables.
  - **Report server TTFB and render time separately. Never a blended
    page-load percentage.** Relative deltas are invariant to client speed only
    when measured on the component that changed. A 30% server improvement on a
    300ms page reads as −22.5% against a 100ms render and −12.9% against a
    400ms one, because the client portion sits in the denominator and never
    improves — so a slow client *understates* the win. Server TTFB is invariant
    and directly comparable to the on-host driver; render time is only ever
    compared against itself on the same box. The absolute figures do not
    matter, which is the existing rule, but which figure the ratio is taken
    over does.
  - **Record the client box's own provenance**: CPU, RAM, Firefox version,
    viewport, turbo and governor state — needed to know that two render numbers
    are comparable at all.
  - Finding 43's floor still applies: a relative claim needs absolute magnitude
    to be resolvable, and below ~100ms the per-measurement sign is unreliable
    whatever units it is expressed in.
  - NUC-2 needs its own quiesce gate. A busy client inflates client-side
    timings exactly as a busy server does.
  - NTP on both boxes, if client-observed timings are ever to be lined up
    against the server's view of the same request.
  - Do not let it replace server-side timing. Browser numbers include CSS
    parsing and JS execution that no Nautobot change touches, which dilutes
    attribution. The browser answers what a user waits for; the on-host driver
    answers which code path got faster.
- **30-sample loopback-vs-LAN comparison under the quiesce gate.** The variability
  claim I used against browser-based timing rests on n=5 with one outlier, taken
  while the box was loaded. It needs measuring or dropping.

## Phase 6 — verification before showing anyone

- [x] `python3 perf/build_report.py --check` exits zero
- [x] **Every number traces, and it is now a committed script rather than a
      one-off.** `perf/verify_report.py` runs 1,385 checks — commit SHAs resolve
      on `perf/recommended`, no finding points at a reverted commit, no baseline
      record is implausible, cumulative figures recompute from their sources,
      and every Reason cell is a finding's own words. Currently: **no problems**
- [ ] Read `report.md` end to end at full size, once, out loud if necessary
- [x] **DONE** (`git merge-base --is-ancestor next perf/recommended` passes). Original: Confirm `perf/recommended` still applies to `upstream/next` at its
      current tip, not the tip recorded at the start of the day
- [x] **DONE** (confirmed; tree hash `90dbd96ffbe...` matches the suite and every arm). Original: Confirm `nautobot/` on `perf/recommended` is byte-identical to the tree
      every Phase 3 number was taken against

## Claims that need evidence before they are made

- **"List views are faster."** Kevin has observed it in dev tools. The
  committed data has one measurement supporting it (finding 01, −12.9% on
  `ui.interface.list`), two changes measured as *not* moving list views
  (finding 07's control: `ui.device.list` and `ui.interface.list` "both within
  noise"; finding 12's control: `ui.interface.list` flat), and the change most
  likely to move them broadly (finding 11) never measured. Query counts cannot
  see any of it. Do not claim it until the Tier 2 A/B lands.
- **"Detail pages are faster."** Supported: 7 of 7 UI detail pages fell 29–52%
  on queries, with wall clock measured on three of them (−24% device detail,
  −25% rack detail, −18.5% device interfaces).
- **Never state "unchanged" from a query count alone.** Every UI list view is
  identical on queries across both arms and at least one of them is
  measurably faster. That is this branch's own central finding pointed at the
  UI, and it is easy to get backwards.

## Cut list, in the order things go

1. **Silk removal from the perf overlay.** Changes every absolute number, so it
   forces a full re-baseline after the A/B runs. One sentence in methodology
   instead.
2. **UI byte-stability normalizer.** Retires a caveat but moves no number.
3. **Request-scoped cache tests** (Phase 5). Defer to the per-finding
   byte-identical controls that already exist.
4. **Whole-apply pair** (Phase 3). The most expensive single run at ~60 min. If
   it goes, the report leads with the two screen aggregates and says the apply
   figure is from an older tree.

Everything above the cut list fits. Roughly **24 points** of local work and
**4h30m** of serial machine time against an 18-hour budget.

## Estimates

| Phase | Points | Machine |
|---|---:|---|
| 0 — long-running work | 0 | 2h18m |
| 1 — instruments | 6 | — |
| 2 — demo branch | 5 | — |
| 3 — measurement runs | 0 | ~2h |
| 4 — report restructure | 8 | — |
| 5 — retire caveats | 5 | ~30m |
| 6 — verification | 0 | — |
| **Total** | **24** | **~4h50m** |
