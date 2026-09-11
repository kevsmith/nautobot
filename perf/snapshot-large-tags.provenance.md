# `perf/snapshot-large-tags.sql` — the campus dataset with tags

**Built 2026-09-11.** The same dataset as `perf/snapshot-large.sql` plus tag rows, for measuring
the tag-dependent code paths the baseline cannot exercise.

    databot generate --archetype enterprise-campus --scale large --seed 42
    databot apply   (client on albert, target hannah, ARM = stock ce01a0464)
    pg_dump -U nautobot -d nautobot

## What differs from `snapshot-large.sql`

Generated from the same archetype, scale and seed, with a databot built 2026-09-11. Diffed
offline against the baseline's own `dataset-large.yaml` before seeding:

| | baseline | tagged |
|---|---|---|
| `extras.tag` | 0 | **5** |
| tag assignments on `dcim.interface` | 0 | **9,274** |
| tag assignments on `dcim.device` | 0 | **116** |
| every other model's row count | — | identical |
| `ipam.vlan` VIDs | — | **136 of 187 renumbered** |

The VID renumbering comes from databot's `region-scoped-vids`, which makes a VID decode to
region + site + purpose. Same count, same names, different numbers. No cost implication is
expected; it is recorded because it is the only other thing that moved.

Device, interface, prefix and location **names are byte-identical** to the baseline, so anything
keyed on a name still resolves.

## Why it was seeded on stock, and why that matters

**The first build of this file was discarded.** It was seeded with the branch armed, and finding
22 stops writing `ObjectChange.object_data` — so all 36,557 change rows had that column NULL
where the baseline carries full JSON blobs. The tell was the dump coming out **smaller** than the
baseline (185 MB against 200 MB) while containing strictly more data.

That would have made the snapshot silently useless for change-log work: `extras-api:objectchange-list`
would read artificially cheap on *both* arms, so stock and branch would look identical on the very
endpoint finding 22 acts on. A reference dataset must not encode which arm created it.

Re-seeded on stock `ce01a0464`. Verified: 36,557 of 36,557 change rows carry `object_data`, and
the dump is 206.7 MB — larger than the baseline, as adding tags to an otherwise identical dataset
should be.

## Restoring

    PERF_SNAPSHOT=perf/snapshot-large-tags.sql perf/scripts/restore_snapshot.sh
    perf/scripts/restore_snapshot.sh                     # the baseline, unchanged

**`expected-counts.txt` cannot tell these two apart.** It records devices, interfaces, cables and
IP addresses, and the tagged dataset matches the baseline on all four — `2902 8925 3278 2937`. So
`run_experiment.sh`'s drift check passes whichever snapshot is loaded, and a measurement taken
against the wrong one would not be caught. `expected-counts-tags.txt` extends the same line with
the two counts that do differ, `5 9390`; check against that file when the tagged snapshot is the
one under test.

## Seeding notes

Two things cost time and are worth knowing before re-running this.

**databot writes nothing to its log until it exits**, even under `PYTHONUNBUFFERED=1`. A 46-minute
run sat at zero bytes and then printed one line. Database row counts are the only usable progress
signal.

**The apply crashed at the end and the data was complete anyway.** It died in
`_create_model_batched -> get_many_by_id` with `httpx.RemoteProtocolError: Server disconnected
without sending a response` — the read-back after the final batch, not the creation. All 75 model
counts verified against the dataset afterwards. Check the counts before assuming a non-zero exit
means a bad seed.

Five models read higher in the database than the dataset declares — `extras.role` 29/45,
`extras.status` 2/22, and the three `vpn` policy/profile models 1/5. Those are Nautobot defaults
created by its own migrations, which databot `$match`ed rather than created. Expected, and present
in the baseline too.
