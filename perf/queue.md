# Work queue

As of 2026-09-06. Ordered, with the reasoning that produced the order — so the
sequence can be argued with rather than just followed.

**Branch state.** `perf/experiments` at `00d47185c` plus this commit.
`perf/verified` at `e29a09f28`, app-code tree byte-identical to
`perf/experiments` (`git rev-parse perf/verified:nautobot` matches). Full suite
green on the current tree: 17,504 tests, `OK (skipped=663, expected failures=1)`,
zero failures, zero errors. 28 accepted findings, of which 8 (seq 28, 32, 33, 35,
37, 38, 39, 40) are instruments or measurement results rather than product changes.

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

`perf/screen_writes.py` + `perf/payloads.py`. 152 of 166 API list endpoints
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

**What it found, which is what makes item 1 more interesting rather than less.**
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

## 1. Investigate the top of the write ranking

The screen produced a ranked list and stopped there, which is what a screen is
for. This is the work it points at.

**Start with `dcim.cable`, now measured at ~220 queries per object** — finding
40 established that the screen's 44.9 is a payload artifact (generated cables
have null terminations and skip every path walk). It is the most expensive
per-object create anywhere on the write surface, 1,648 rows in the datacenter
dataset, and 0.34 seconds each.

**Then `ipam.ipaddresstointerface` (65.6 marginal q/obj).** Both have a single SQL shape repeated far out of proportion to the
work — cable repeats one `SELECT` on `dcim_cabletocabletermination` **280 times
per ten cables**, which is the same table finding 36 just prefetched on the read
side. That is a strong hint the write path has an analogue of the read fix, and
it is the cheapest thing on this list to test.

**`dcim.device` at 40.0 is the one with the most leverage** — it is the model a
real bulk import creates most of, and it carries 309ms of database time on ten
objects. `dcim.interface` at 23.8 is second on the same reasoning.

**Rank on database time as well as on queries.** Exactly the gap finding 37 found
in the read screen, present here for the same reason and fixable the same way:
`db_ms` is already recorded per measurement, so it is a sort key, not a re-run.

**Raise coverage where it is cheap.** 19 of the 47 unmeasured models are
unmeasurable only because this dataset has no rows of a required related model —
cluster, moduletype, cloudnetwork, virtualserver, savedview. Seeding a handful of
rows converts them from "not measured" to measured without touching the payload
builder. The other 28 are model validation and are not worth chasing.

## 2. Affordance-adoption screen

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

**Ordered after item 1 only because writes are the unexplored axis, and item 1
now has a ranked list pointing at specific endpoints where this has none.** On
expected value per hour this may still beat it, and it is cheaper. Reasonable to
swap.

## 3. Finding 35 audit — undecided, needs a call

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


## 4. Read-side leftovers, now explicitly ranked below the write path

Kept as one item rather than four, because finding 37 established that none of
it competes with items 1–3. Ordered within itself by what it would teach.

- **Rank the screen on database time, not only on queries per object.** `dcim.device`
  list is 8 queries, zero duplicates, and **438ms of database time** on a page of
  25 — 55ms per query, the largest read-side db-time item there is, reproducible
  across both runs (432 / 438 / 441ms). At 0.32 q/obj the ranking puts it 100th.
  Every read fix on this branch has been a query-count fix, and this endpoint has
  almost no queries to remove. `db_ms` is already recorded, so the ranking change
  is one line; the investigation behind it is not.
- **Fix the screen's coverage accounting.** It measures 166 list endpoints and
  exercises 49. Seventy return zero rows against this dataset — `cluster`,
  `virtualmachine`, `vminterface`, `module` and the whole modules/templates
  family, `tag`, `service`, `rir` — and a zero-row endpoint reads as cheap when
  it is unmeasured. This is the failure item 2 is required to avoid, present in
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

**On carrying finding 22 on `perf/verified`.** That branch's job is a clean read
on the cumulative effect, and the reason for keeping 22 off it was that a changed
API payload could break the databot apply producing the write-path evidence. That
evidence is now collected (findings 39, 40), and the change was measured on both
arms with `screen_writes.py` before landing — 102 models measured either side,
identical coverage — so the objection has expired.

**The largest measured gap is still environment, not code** — see the section of
that name in `perf/report.md`. It should be re-measured now that both sides can
be taken at concurrency 1 under uwsgi.

**Process rules that cost time to learn** are in `perf/README.md` and the commit
messages. The two most recently earned: a process can look alive in `ps` and be
doing nothing, so check consumed CPU rather than elapsed time; and `invoke
tests --no-keepdb` blocks on a confirmation prompt unless `--no-input` is also
passed.
