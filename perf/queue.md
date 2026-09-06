# Work queue

As of 2026-09-05. Ordered, with the reasoning that produced the order — so the
sequence can be argued with rather than just followed.

**Branch state.** `perf/experiments` at `bd786e8b7`. `perf/verified` at
`e29a09f28`, app-code tree byte-identical to `perf/experiments`
(`git rev-parse perf/verified:nautobot` matches). Full suite green on the
current tree: 17,504 tests, `OK (skipped=663, expected failures=1)`, zero
failures, zero errors. 24 accepted findings, of which 4 (seq 28, 32, 33, 35) are instruments or measurement results rather than product changes.

---

## 1. Re-run the read screening pass

    perf/dc.sh exec -T nautobot python /source/perf/screen_reads.py

**Why first.** 84 seconds, and four fixes have landed since the screen last ran
(findings 30, 31, 34, 36). Its output is a ranked list of where to point the
next investigation, so a stale ranking misdirects everything below it. Cheap
enough that not running it first is the more expensive choice.

**Definition of done.** A fresh ranking, and an explicit check of whether
anything on the read side still outranks the write path for attention. If it
does, this queue is wrong below here and should be re-cut.

**Known residuals from the last run**, all still untouched:
`vpn.vpntunnelendpoint?depth=1` 9.8 q/obj · `circuits.circuit?depth=1` 7.3 ·
`dcim.interfaceredundancygroupassociation?depth=1` 6.6 ·
`ipam.ipaddresstointerface?depth=1` 5.9.

## 2. Attribute the −37% on the write path

Kevin measured a large datacenter dataset apply at **495s against
`perf/verified` versus 786s against stock `next`** — his own hardware, his own
dataset, timed to completion on both sides.

**Why it matters more than its size.** It is the only evidence this branch has
that the write path carries real wins, and no instrument here would have
predicted it. Tier 1W is eleven hand-picked ORM operations over four models.

**The hypothesis, which is his and is well supported.** Three accepted findings
measured wins specifically on bulk create, all on the change-record
serialization path:

| finding | change | measured |
|---|---|---|
| 13 | reuse one API serializer per model when change-logging a transaction | −28% bulk create |
| 4 | memoize natural-key lookups for the duration of one serialization | −13% bulk create (within variance) |
| 31 | fix the tag cache in `serialize_object` | −6.9% loop / −5.6% deferred |

**The test.** Revert finding 13 alone on `perf/verified`, re-time the apply. If
it returns toward 786s the win is concentrated and the write matrix should hunt
for finding-13-shaped work. If it barely moves, the win is diffuse across the
natural-key work and the matrix is a breadth exercise. **That answer changes
what item 3 is for, which is why it comes first.**

**Caveat to fix while here.** Both figures are single runs. Re-time at least
once more per side.

## 3. Build the write screening matrix

**The blocker is gone.** Finding 33 made a database reset 1.3s (template clone,
`perf/reset_db.sh`), so the matrix can afford a reset per operation rather than
batching around one. Finding 29's "~70 seconds per arm" objection was priced
against the 49s slow path and is corrected in the record.

**What is actually left to build:**

- **Schema-valid payloads per model.** databot generates these from the OpenAPI
  schema and OPTIONS metadata. This is the hard part and the whole remaining
  risk.
- **Coverage accounting, non-negotiable.** A model whose generated payload
  fails validation drops out silently and reads as *not a problem* rather than
  *not measured*. The output must carry attempted / valid-payload / measured
  counts per model. Without it this repeats the failure that left "330
  endpoints, roughly 5%" in `perf/README.md` for weeks — a coverage number
  nobody had checked.
- **The warmup convention from finding 33.** Discard exactly one request after
  every reset. Position 1 after a clone ran 981.8ms median against a 706.4ms
  warm control and varied 719.6 / 981.8 / 1138.9ms across rounds; position 2 is
  indistinguishable from warm. Skipping this gives every model a variable
  few-hundred-millisecond bias, in the instrument built to make those numbers
  trustworthy.

**Isolation model** (finding 29, unchanged): the in-process ORM half can keep
rolled-back transactions — commit vs rollback is −0.9%, inside variance. Only
the REST half needs restore-based orchestration, because REST writes cross the
process boundary.

## 4. Affordance-adoption screen

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

**Ordered after item 3 only because writes are the unexplored axis.** On
expected value per hour this may well beat it, and it is cheaper. Reasonable to
swap.

## 5. Finding 35 audit — undecided, needs a call

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

---

## Standing context

**Parked, and the largest untapped write-path item.** Findings 16 and 22 both
attack the v1/v2 double-serialization in `to_objectchange()` — every change
record is serialized twice, and the v1 copy is only ever read as a fallback that
never fires for new records. Measured at −200 queries per 100 writes, −14.2% on
bulk create. Both are Tier C: two public API payloads change and 13 tests break,
so landing either needs a deprecation cycle. Kevin's −37% is the first evidence
that conversation is worth having.

**Do not carry these on `perf/verified`.** Its job is a clean read on the
cumulative effect, and a changed API payload could break the databot apply that
is producing the write-path evidence.

**The largest measured gap is still environment, not code** — see the section of
that name in `perf/report.md`. It should be re-measured now that both sides can
be taken at concurrency 1 under uwsgi.

**Process rules that cost time to learn** are in `perf/README.md` and the commit
messages. The two most recently earned: a process can look alive in `ps` and be
doing nothing, so check consumed CPU rather than elapsed time; and `invoke
tests --no-keepdb` blocks on a confirmation prompt unless `--no-input` is also
passed.
