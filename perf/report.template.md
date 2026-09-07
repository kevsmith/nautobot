# Optimization opportunities in Nautobot core

Measured read and write path optimizations, each one applied to a branch and priced. Every
number came from a reproducible harness against a fixed dataset on one dedicated host. The
instruments, the baselines and the working behind each change are in
[`perf/methodology.md`](methodology.md).

<!--GEN:factbar-->

<!--GEN:provenance-->

## Cumulative effect

<!--GEN:cumulative-->

## Tiers

Tiers price the adoption cost rather than filter on it. Nothing here is disqualified for being
expensive; it is labelled so the price is visible. A flag alongside a tier carries the part of
the risk the tier cannot express.

<!--GEN:tiers-->

<!--GEN:findings-->

## Methodology

An isolated stack (`DEBUG=False`, `PLUGINS = []`, pinned CPU and memory, served under uwsgi)
against databot `enterprise-campus / large / seed 42` (24,091 objects) for reads and
`datacenter / large` (11,578 rows) for the whole-workflow write measurements. Every aggregate
above is a stock-versus-branch delta taken on one host with the arms alternated and both trees
proved different by content hash before each arm, computed over measurements present on both
sides. The read figure comes from a 57-scenario loop that measures each list view twice: the
document, and the separate request the browser fires to render its rows. The write figures come
from a screen enumerating every POST endpoint the URL resolver exposes, plus one end-to-end
apply that has no read equivalent. A whole test suite of 17,505 tests passed against the same
tree content hash these measurements were taken against. Absolute figures are not comparable to
any other machine.

## Observations

- Query count cannot see this work. The request that renders a list view's rows got faster on
  **18 of 18 pages, a median 18.6%**, while its query count stayed identical on every one of
  them. The time removed is almost exactly proportional to how much rendering a page does
  (**correlation 0.97** between a page's cost and the milliseconds saved), which is what
  removing a per-row overhead looks like. The rejected FK pre-warm did the reverse: 666 fewer
  queries, 62% slower.
- The database is not the bottleneck. PostgreSQL executed 325,152 queries in 4.6 seconds across
  a full baseline run, so every second of user-visible latency measured here is Python-side and
  addressable in application code.
- Five findings exist only because an affordance was added and its call sites were never
  updated, the one pattern here with a repeatable cause and a far smaller search space to screen
  than 166 endpoints by cost.

---

<!--GEN:endnote-->
