# Most impactful fixes

As of 2026-10-28.

## Approved upstream: Device LLDP Neighbors tab (#9693)

Issue [#9692](https://github.com/nautobot/nautobot/issues/9692), PR
[#9693](https://github.com/nautobot/nautobot/pull/9693). Approved, not yet merged. Finding 69.

The tab fetched each interface's connected endpoint and that endpoint's device with separate queries, and
fetched the peer's virtual chassis and a circuit's provider the same way.

- On the perf dataset, for a device with 49 interfaces: 875 -> 34 queries, and 1,320.7 -> 246.4 ms (−81.3%).
  This is the largest single-page reduction in the perf work.
- On develop's test fixtures: 1,123 -> 47 queries at 49 cabled interfaces. The count no longer grows with
  the number of interfaces.

The largest merged fix is #9681's ContentTypesColumn change: the roles list went from 224.1 -> 155.8 ms
(−30.5%), and the statuses list from 132.7 -> 96.2 ms (−27.5%).

## Only in our fork: prefetch depth-0 foreign keys in the REST optimizer (finding 62)

On `perf/recommended`, planned for PR 11. Ported from Ken Celenza's `2faf6adad`.

At `?depth=0` the API renders each related object as a link or a natural key and never reads its columns.
The optimizer still joins every foreign key. On Device that is 16 joins, which widen each row from 32
columns to 212. With this change, the optimizer prefetches each related object once.

- Device list API: 684 -> 250 ms (−63.4%). Every round of the change was faster than every round of the
  baseline.
- Device detail API: 171 -> 130 ms (−24.2%).
- All 11 API scenarios: 6,148 -> 5,076 ms (−17.4%).
- Database time over 516 REST read measurements: 4,746 -> 4,000 ms (−15.7%).

The query count goes up by design, +5.4% over those 516 measurements, because each prefetched relation is
one more query. A reviewer who reads only the query count sees a regression. The case for the change is the
wall clock and the database time.

Findings 90 and 91 together have a larger effect. At `?depth=2` they approximately double the throughput
for devices, IP addresses and prefixes: at one client, the device list goes from 0.49 -> 1.06 req/s (+116%).
Both are held off `perf/recommended`, because they need psycopg 3 in the lockfile and the full test suite
has not run on them.
