# The harness

`perf/README.md` is the authority on *what* this exercise measures and *why* — scope, gate
semantics, the risk taxonomy, the optimization loop. This file is the authority on *how*: what each
script does, which ones compose, and the operational traps that have cost real time.

Read `perf/README.md` first. Nothing here overrides it.

## Three layers

    instruments   answer one question about one thing. Cheap, disposable, ~45 of them.
    harness       make a measurement trustworthy: lock it, gate it, prove the tree, alternate arms.
    reporting     turn findings into perf/report.md, and refuse to publish what the data cannot support.

The harness layer is small and its rules are not negotiable. The instrument layer is large and
deliberately throwaway — a probe that answered its question and was never run again is a success,
not debt.

## Harness

| script | does |
|---|---|
| `dc.sh` | the only correct `docker compose` invocation for this stack. Derives `NAUTOBOT_VER` from pyproject and pins the project name and four compose files, including the perf overlay. **Never call `docker compose` directly** — the overlay is what pins cpusets and serves under uwsgi. |
| `sync.sh` | push the working tree to the measurement host and *prove* both sides match by content hash. `--restart` restarts the app and waits for load to settle. rsync's exit code is a claim; the hash is the verification. |
| `quiesce.sh` | refuse to let a wall-clock number be taken on a busy box. Checks loadavg, CPU governor, turbo, and that the containers are up. Hard-fails on a wrong governor. |
| `measure.sh` | take an exclusive lock so exactly one timing run is ever in flight, gate on `quiesce.sh`, and record the tree that produced the number. Wrap **every** wall-clock measurement in this. |
| `arm_control.sh` | `arm <ref>` swaps `nautobot/` to a git ref, restarts, waits for load to fall, and prints the content hash that proves the arms differ. `hash` prints it alone. `reset` clones an empty database. Removes files the target ref does not have, which a path-scoped checkout cannot. |
| `run_experiment.sh` | one experiment end to end: dataset-drift check, Tier 1, then `compare.py` for a verdict. |
| `run_screen_ab.sh` | one screen (`reads` or `writes`) against both arms, alternating order, restoring the tree on exit and on failure. |
| `apply_arm.sh` | one arm of the whole-workflow apply: reset the database, swap the tree, run a timed `databot apply`. **Destructive.** Refuses a remote client paired with a localhost URL. |
| `reset_db.sh`, `restore_snapshot.sh` | return the database to empty (about a second, template clone) or to the seeded baseline. |
| `record_expected_counts.sh` | record object counts so `run_experiment.sh` can detect dataset drift. |

## Measurement tiers

| script | tier | what it is |
|---|---|---|
| `tier1_queries.py` | 1 | deterministic per-endpoint query counts, in-process via the Django test client. Also dumps `urls.json` for Tier 2. |
| `tier1w_writes.py` | 1W | the same for the write path, every operation rolled back. |
| `tier2_latency.py` | 2 | per-endpoint wall clock over HTTP via cassowary. Needs a **session cookie** for UI views; an API token authenticates DRF only and 403s on UI endpoints. |
| `screen_reads.py`, `screen_writes.py` | screen | enumerate every REST read/write endpoint from the URL resolver at run time, normalised to cost per object. Screening instruments, **not** regression gates. |
| `workload.py` | — | resolves `perf/workload.yml` into runnable URLs. `DEFAULT_WORKLOAD` is the shared path constant; three instruments broke when it did not exist. |

## The loop and the screens are different instruments

This distinction is the one most likely to produce a wrong number, because both measure "reads"
and they answer different questions.

**The read loop** is `tier1_queries.py` + `tier2_latency.py` over `perf/workload.yml` — 57
scenarios naming specific pages and endpoints. It includes **UI pages**: list views, detail pages,
the home page, and the 18 row-rendering HTMX requests. Its wall clock comes from Tier 2, over HTTP,
which is what a user actually waits for.

**The read screen** is `screen_reads.py`, which enumerates every REST read endpoint from the URL
resolver at run time via `api_list_views()` and normalises to cost per object. It is **REST-only** —
no UI page appears in it at all — and it runs in-process.

|  | read loop | read screen |
|---|---|---|
| coverage | 57 named scenarios | every REST read endpoint (~300) |
| includes UI | **yes** — list, detail, home, HTMX rows | **no**, API only |
| wall clock | Tier 2, over HTTP | in-process |
| metric | absolute per scenario | normalised per object |
| selection | hand-picked for diagnostic interest | exhaustive from the resolver |
| built for | tracking a workload; the cumulative table | ranking where to look next |

**Neither substitutes for the other, and the reason is the selection.** The loop's scenarios are
largely the ones this branch has optimised, so its aggregate is favourable by construction — a fair
description of those pages, not a prediction for an arbitrary one. The screen is unbiased over the
REST surface and is the only instrument that can say whether a gain generalises to the endpoints
nobody has looked at. But it cannot speak for the UI, and 14 of the 43 accepted findings rest on UI
evidence.

Two practical consequences:

*Do not swap one for the other in the cumulative table.* Replacing the loop with the screen would
remove every UI page from the branch's headline result.

*Do not read the screen as a gate.* Its own header says it: a screening instrument, run
occasionally, read as a ranked list. `compare.py` against a Tier 1 baseline is the gate.

The same split applies on the write path, with one difference: there is no write *loop*. Tier 1W
covers a handful of operations, and `screen_writes.py` covers every POST endpoint — so the write
row in the cumulative table is a screen, while the read row is a loop. That asymmetry is real and
worth knowing when comparing the two rows against each other.

## Comparison and reporting

| script | does |
|---|---|
| `compare.py` | Tier 1 against a committed baseline. Exit 0 pass, 1 query regression, 2 endpoint broken. A regression is a **tripwire, not a verdict**. |
| `compare_screen.py` | diff two screen runs. Matches on **`(id, kind)`**, aggregates only over measurements present *and* successful on both arms, and prints what fell out. |
| `median_rounds.py` | collapse N rounds of one arm into per-measurement medians. `compare_screen.py` takes one file per arm, so without this an aggregate rests on a single round. |
| `build_report.py` | render `perf/report.md` and `perf/methodology.md` from `perf/findings/*.yml` plus the baselines. `--check` verifies they are current. **Never hand-edit the outputs.** |
| `verify_report.py` | verify the report says nothing the committed data cannot support. |
| `build_branch.py` | generate `perf/recommended`, the upstream-facing replay, with messages composed from the findings. |

## Instruments worth knowing

Most probes are single-question and named for it. These generalise:

| script | question |
|---|---|
| `attribute.py` | which Python line issues a scenario's queries. Honours the workload's headers, and skips plumbing frames so a queryset override does not absorb the attribution. |
| `probe_page_phases.py` | where a page's wall clock goes: view work, template render, database. Takes scenario ids. |
| `probe_query_compilations.py` | SQL **compiled** versus **executed** — the counter that made finding 55 visible. |
| `probe_discarded_compilations.py` | classifies compilations: executed, nested subquery, or genuinely discarded. Use before believing `compiled − executed` is waste. |
| `probe_endpoint_ab.py` | query count, wall clock and body digest for arbitrary URLs. |
| `probe_table_columns.py` | per-cell cost by column class, accessor split from render. |
| `probe_form_widgets.py` | widget rendering by widget class, with option counts. |
| `probe_write_attribution.py` | the queries of a create for any model, by call site. |
| `payloads.py` | a minimal schema-valid REST payload for any model. |
| `capture_proxy.py` | sits between databot and Nautobot and keeps whatever fails. |

## Recipes

### Attribute before forming a hypothesis

    perf/scripts/sync.sh --restart
    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/attribute.py <scenario-id>
    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_page_phases.py <ids...>

### One A/B experiment

Commit the change on a scratch branch, keep the baseline on the branch tip, then alternate. Three
rounds, order reversed between them:

    round 1:  A  B        sequence: A B B A A B
    round 2:  B  A        with three rounds the lead is 2-1, not even;
    round 3:  A  B        the controls say whether that mattered

Per arm: `arm_control.sh arm <ref>`, record the hash, then `measure.sh <instrument>`. Always include
a **control** the change cannot touch — it is what sets the noise floor. Prove the guard in both
directions: run the shipped test with the fix reverted.

### Stock versus branch

Stock is `next` at `ce01a0464`. Arming to it strips branch-added files, so the arm hashes next's own
`e308df687`. If it hashes anything else, the arm is not stock.

### Overnight cumulative refresh

Order is not arbitrary: the apply resets the database and destroys the dataset the other two
measure against.

    1. read loop     tier1_queries.py + tier2_latency.py, 57 scenarios, 3 alternating rounds
    2. write screen  run_screen_ab.sh writes <stock> <branch> --rounds 3
    3. apply         apply_arm.sh, 4 arms, stock/branch/branch/stock    <-- DESTRUCTIVE
    4. restore       restore_snapshot.sh, then verify against expected-counts.txt

Then `median_rounds.py` per arm, `compare_screen.py` for the writes row, update
`perf/baselines/cumulative.json`, and `build_report.py`. Roughly five hours for all four parts.

## Traps

Every one of these has cost real time.

**Never sync while a measurement is in flight.** `sync.sh` rsyncs with `--delete` and will replace
the armed tree mid-arm. Local work is safe during a measurement; pushing it is not.

**Detach anything long.** `setsid nohup <cmd> > log 2>&1 < /dev/null &`. A 2h15m suite was lost to
`| tail -60` through a live ssh, because `tail` buffers to EOF and the supervisor reaped the
process. Detached jobs have since survived four supervisor kills without noticing.

**Read the summary line, never the exit status.** Three runs on this branch exited 0 while failing,
and one exited 1 after succeeding. For a test suite that means `Ran N tests` and `OK`/`FAILED`.

**Do not count progress characters.** `grep -oE "[.sF]"` matches those letters inside log prose and
will report hundreds of phantom failures. Match the runner's own summary, or wait for it.

**The container writes bind-mounted files as root.** A script run via `dc.sh exec` that writes into
`/source/perf/results/` leaves a root-owned file, and any `.py` there joins `sync.sh`'s hash set and
breaks the tree gate. Clean up, or write outside the repo.

**`pkill -f <pattern>` can match your own ssh command line** and kill the shell you are running in.
Resolve PIDs first, then kill by PID.

**Tier 2 credentials do not survive a database restore.** A session lives in `django_session`, so
`restore_snapshot.sh` and `arm_control.sh reset` wipe it; the API token is in the snapshot and has
no expiry, so it survives. A Tier 2 A/B once completed cleanly having timed **one of eight
scenarios** because the session had been minted before an overnight restore — the probe correctly
refuses to time an endpoint returning 302, so the run looked like it had worked. `restore_snapshot.sh`
now ends by calling `ensure_credentials.py`, which mints a **fixed-key** session so scripts can
hardcode it. Verify the cookie returns 200 before launching anything long regardless.

**A restore used to leave celery running**, which fails `quiesce.sh` ("NOT QUIET, extra:
celery_beat celery_worker") because the measurement configuration is exactly db, nautobot and
redis. `restore_snapshot.sh` now restores only the services that were running before it. If a gate
refuses on unexpected containers, check what last touched the stack rather than the gate.

**`urls.json` goes stale.** Its `pick:` strategies resolve concrete primary keys; when one stops
existing the endpoint 404s and Tier 2 correctly refuses to time it — silently shrinking coverage.
Regenerate before any comparison that matters.

**Tier 2 concurrency defaults to 4 against 3 uwsgi workers**, which queues and roughly doubles p95.
Use `-c 1` unless you are deliberately measuring under concurrency.

**Do not mix tiers in one claim.** The same page is about 161 ms in-process and 202 ms over HTTP, so
a fixed saving is a different percentage of each. The report's cumulative table is Tier 2.

**`compare_screen.py` keys on `(id, kind)`.** Keying on `id` alone collapsed 257 measurements to 105
and made a good run look broken.

**`build_report.py` appends its own computed statistics to an aggregate's `note`.** Write narrative
and provenance there, not numbers it derives, or the rendered row says everything twice.

**`median_rounds.py` carries booleans from round 1, silently.** It builds each merged record as
`dict(rows[0])` and takes a median only of fields that are numeric and *not* `bool`, so every
boolean in a median file describes **the first round alone**. `query_count_stable` is the one that
matters: a published `unstable_query_count: 0` is not a three-round claim, and a measurement that
was stable in round 1 and jittered later reads as clean. Check the flag against the per-round files
rather than the median when it decides anything.

**`compare_screen.py` counts instability on the current arm only** (`curr[k]`, line 166). A
baseline-arm jitter does not appear in `unstable_query_count` at all. That is the right default —
the gate is about whether *this* arm's numbers can be trusted — but it means "0 unstable" says
nothing about stock. On 2026-09-10 the read screen's stock arm jittered by one query on three
`list.depth1` endpoints (181→182, 741→742, 767→768), one per round and a different endpoint each
time, while the branch arm was bit-identical across all three rounds. Nothing was wrong; but
reading the raw files and the gate figure as the same quantity briefly suggested three
disqualified rows.

**An unrun script rots.** `run_screen_ab.sh` carried two defects — a path stale since the harness
moved into `perf/scripts/`, and arms that never actually alternated despite its own header requiring
it — because nothing had run it since. Grep for the shape after any reorganisation.

**A test that reports a confident number may be exercising nothing.** A regression test on the home
page passed because the panel it targeted rendered "No permission" for the permissionless test user.
Assert your preconditions: that the thing rendered, and that it rendered *your* data.

**Micro-benchmarks of request-scoped code measure the wrong path.** `get_settings_or_config` costs
211–219 µs standalone and 2.38 µs inside a request, because the cache is per-request. Anything
priced in a bare loop deserves re-checking inside a request before it justifies a change.

**Aggregates point the wrong way.** Three times in one day a total looked like a systemic cost and
attribution found a single culprit: 61.2 ms of `<string>` renders that was not compilation, a 224 µs
config read that is 2.38 µs where it matters, and 19.8 ms of option rendering that was one field
with 130 options. Attribute before you fix.

## Environment

    measurement host   hannah   serves Nautobot, holds the dataset, runs the harness
    client host        albert   drives databot and any off-box load generation
    stock ref          next @ ce01a0464     arms to content hash e308df687
    app                http://localhost:8180 (harness default); :8080 published for browsers
    containers         nautobot cpuset 0,1,4,5 · db 2,6 · redis/celery 3,7
    dataset            2902 devices / 8925 interfaces / 3278 cables / 2937 IPs
                       (perf/baselines/expected-counts.txt)

**Not in the dataset:** VirtualMachine, Cluster and VirtualDeviceContext are all at zero rows, and
JobResult has two. Several queue candidates cannot be priced until the generator creates them.
