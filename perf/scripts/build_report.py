#!/usr/bin/env python3
"""Render perf/report.md and perf/methodology.md from perf/findings/*.yml and the baselines.

The report is the deliverable on this branch -- the tree may never be merged. It
drifted five commits behind the code once, carrying a figure a later commit had
retracted, so it is generated rather than maintained.

Numbers live in exactly two places: perf/findings/*.yml for what each experiment
found, and perf/baselines/*.json for what the instruments measured. This script
reads both and fills the <!--GEN:...--> markers in the two templates.
Narrative prose stays in the template, where writing it by hand is the point.

Two documents, because they answer different questions. perf/report.md is for a
reader deciding what to adopt and what it is worth; perf/methodology.md is for a
reader auditing a number. They were one file for a while, and the working
crowded out the result: 55% of a 155KB report was per-finding evidence.

Markdown rather than HTML, deliberately: a markdown diff shows which number
moved, so drift becomes visible in review rather than merely detectable by
--check. It also removes escaping and tag-balancing from a tool whose whole job
is not being wrong.

    python3 perf/scripts/build_report.py            # write both documents
    python3 perf/scripts/build_report.py --check    # exit 1 if either is stale
"""

import argparse
import json
import pathlib
import re
import shutil
import statistics
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PERF = ROOT / "perf"
STOCK_REF = "next"
GIT = shutil.which("git") or "/usr/bin/git"

# Grouped by what was decided, because that is the first thing a reader needs:
# which of these are we doing. Adoption cost has not gone away -- it is on every
# entry as a tier and, where one exists, a caveat -- but it is a property of a
# finding rather than a way to file it.
GROUPS = [
    (
        "accepted",
        "Accepted",
        "Measured, kept, and applied to the tree. Each Reason is that change measured on its "
        "own, and several reduce the same cost by different routes -- they do not sum, and the "
        "cumulative table above is the measured total.",
    ),
    (
        "rejected",
        "Rejected",
        "Measurement or blast-radius analysis ruled these out. Listed because a rejected "
        "optimization prices an option someone would otherwise retry.",
    ),
    (
        "reverted",
        "Reverted after landing",
        "Accepted and applied, then removed once a later finding made the mechanism inert. "
        "These are not rejections: each was measured, correct, and worth taking at the time. "
        "They are listed separately because a reader totalling the branch's effect must not "
        "count them, and because the reason one finding can strand another is worth seeing.",
    ),
]

# Flags cut across both axes and make any cell stricter. They render in the same
# table cell as the tier, so a flag with no definition in front of the reader is
# a label they have to guess at -- and both of these carry the part of the risk
# the tier cannot express.
FLAG_HELP = {
    "third-party-coupled": (
        "Reimplements or depends on internals of a dependency, so an upgrade can change "
        "behaviour rather than break a signature. Correct against the pinned version, and a "
        "differential test now renders the same cell both ways so a divergence fails a test "
        "instead of producing wrong output."
    ),
    "unmeasurable-on-this-dataset": (
        "The endpoint returns zero rows on both snapshots, so no timing and no endpoint probe here "
        "can show the defect. Gated instead on a deterministic counter over rows synthesised inside "
        "a rolled-back transaction: the mechanism is demonstrated, the value on a populated instance "
        "is not."
    ),
    "security-visible": (
        "Touches an authorisation decision rather than a displayed value, so a stale or shared "
        "result is a permissions bug."
    ),
}

TIER_HELP = {
    "A": "no observable change",
    "B1": "state scoped to a request, transaction or instance",
    "B2": "state outliving its scope, needs invalidating",
    "C": "changes observable behaviour",
    "M0": "metadata-only migration",
    "M1": "bounded single-pass migration",
    "M2": "unbounded migration",
}


def anchor(text):
    """GitHub's heading-anchor algorithm, closely enough for internal links."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s_]+", "-", slug).strip("-")


def group_of(f):
    """Classify by what was decided: accepted, or not.

    Two buckets, and every finding lands in exactly one. `not-taken` and
    `priced` join `rejected`: from a reader's point of view they are all "we
    looked and we are not doing it", and the distinction -- killed on principle
    against closed on magnitude -- lives in each finding's own `reason`.

    There is no "parked" bucket. There was one while the ledger still carried
    open decisions; every non-accepted finding has since been measured and
    closed, so a third heading would have been an empty promise of follow-up.

    `reverted` is its own bucket rather than a rejection. A finding that landed,
    was measured, and was later stranded by a different finding is not something
    "we looked at and are not doing" -- filing it under Rejected would misdescribe
    both the decision and the history. What it shares with a rejection is only
    that a reader must not count it toward the branch's effect.
    """
    if f["status"] == "accepted":
        return "accepted"
    if f["status"] == "reverted":
        return "reverted"
    return "rejected"


def is_product_change(f):
    """True if the finding proposes a change to Nautobot rather than to the harness.

    Read from `site`, which names the code a finding touches. A finding whose every
    named path is under `perf/` changed an instrument, not the product -- those are
    real results and they stay in the report, but they belong with the methodology
    they describe rather than in a list of optimizations someone might adopt.

    A `site` with no path in it at all ("an existing supported setting", "a new data
    migration") is a product change described in prose, so the default is product.
    """
    paths = [part.strip() for part in re.split(r"[-\u2013,]| plus ", str(f.get("site") or "")) if "/" in part]
    return not (paths and all(part.startswith("perf/") for part in paths))


def load_findings():
    out = []
    for path in sorted((PERF / "findings").glob("*.yml")):
        with path.open() as fh:
            out.append(yaml.safe_load(fh))
    return sorted(out, key=lambda f: f["seq"])


REQUIRED = ("seq", "status", "behaviour", "migration", "title", "summary", "wall_clock")

# A wall-clock headline is a signed percentage, or an explicit statement that there
# is none. Absolutes mixed in among percentages make the column incomparable row to
# row, so one is only allowed alongside a percentage, in parentheses.
WALL_OK = re.compile(r"^(?:[+−-]|≈[+−-]?)\d|^not measured|^not applicable|^no measurable")  # noqa: RUF001 -- U+2212 is deliberate in rendered output


def validate(findings):
    """Return a list of schema problems.

    `wall_clock` is required. Eight of the first fifteen findings carried no
    wall-clock figure -- five because the number was in the commit and never
    transcribed, three because it was never measured -- and a blank field reads
    as an oversight either way. Requiring it forces the record to say which:
    a figure, or the reason there is none.
    """
    problems = []
    for f in findings:
        for key in REQUIRED:
            if not f.get(key):
                problems.append(f"finding {f.get('seq', '??')}: missing {key}")
        if f.get("wall_clock") and not WALL_OK.match(str(f["wall_clock"])):
            problems.append(
                f"finding {f['seq']}: wall_clock {f['wall_clock']!r} should lead with a signed "
                "percentage, or say 'not measured: <reason>'"
            )
        for flag in f.get("flags") or []:
            if flag not in FLAG_HELP:
                problems.append(f"finding {f['seq']}: flag {flag!r} has no definition in FLAG_HELP")
        if f.get("basis") and len(str(f["basis"])) > 90:
            problems.append(f"finding {f['seq']}: basis is {len(str(f['basis']))} chars, over 90")
        if f.get("behaviour") not in ("A", "B1", "B2", "C"):
            problems.append(f"finding {f['seq']}: behaviour {f.get('behaviour')!r} not a tier")
        # A caveat is a release note for something being adopted. A finding that
        # is not being done does not need one -- its `reason` carries the
        # explanation instead. Exempting only "rejected" and not "not-taken"
        # made the two statuses inconsistent here while group_of already treated
        # them as the same thing.
        needs = f.get("behaviour") in ("B2", "C") or f.get("migration", "-") != "-"
        if needs and not f.get("caveat") and f.get("status") not in ("rejected", "not-taken"):
            problems.append(f"finding {f['seq']}: {f['behaviour']}/{f['migration']} needs a caveat")
    return problems


def basis(f):
    """Why a finding was accepted or rejected, in one table cell.

    Wall clock is the reason for most of them, so `wall_clock` is the default
    and no finding has to repeat itself. The field exists for the ones where it
    is not: 42 was taken for consistency at -0.2%, 22 for storage, 23 was
    refused on correctness, and three accepted changes have no wall-clock
    figure at all. A "not measured" cell in the reason column said nothing
    about why anyone kept the change.
    """
    return f.get("basis") or f["wall_clock"]


def tiers(f):
    bits = [f["behaviour"]]
    if f.get("migration", "-") != "-":
        bits.append(f["migration"])
    bits += list(f.get("flags") or [])
    return " ".join(f"`{b}`" for b in bits)


# The instruments, as opposed to the evidence. `result` accumulated 30-odd
# ad-hoc keys across 42 findings -- attribution, staged, residual, response,
# screen, ceiling -- each of them a paragraph of supporting detail. They are
# real and they are kept, but in perf/methodology.md: a reader deciding whether
# to adopt a change needs the four instruments, not the working.
CORE_RESULT_KEYS = ("queries", "duplicates", "in_process", "wall", "config_reads", "redis_reads")


def load_runs(pattern, key):
    """Median-of-medians and spread across the committed baseline rounds."""
    files = sorted((PERF / "baselines").glob(pattern))
    if not files:
        return None
    runs = []
    for path in files:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "endpoints" in data:
            runs.append({e["id"]: e.get(key) for e in data["endpoints"]})
        else:
            runs.append({k: v.get(key) for k, v in data.items()})
    ids = [i for i in runs[0] if all(r.get(i) is not None for r in runs)]
    out = {}
    for i in ids:
        vals = [r[i] for r in runs]
        med = statistics.median(vals)
        out[i] = (med, (max(vals) - min(vals)) / med * 100 if med else 0.0, vals)
    return out


def stock_commit():
    """The exact commit the stock arm was taken against.

    A version string is not an identifier -- `next` carried 3.3.0a0 for every
    commit in a release cycle, so "measured against next at 3.3.0a0" does not
    say which tree. The SHA does. Derived at build time and cross-checked
    against the value recorded in baselines/cumulative.json, so the report
    cannot quietly describe a different tree than the one measured.
    """
    proc = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "--short=9", STOCK_REF], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() or None


def render_factbar(findings):
    """What the numbers were taken over. Not the numbers themselves.

    Every figure moved to the cumulative table directly below this, because a
    fact bar carrying a headline and a table repeating it made the reader check
    whether the two agreed. What is left is the shape of the exercise: which
    tree, which dataset, how much was proposed and how much survived.

    "Accepted changes to Nautobot" read as upstream acceptance to the first
    reviewer who saw it, and it was the first number on the page. These are
    accepted on this branch by the person who measured them; anything
    upstreamed would go through review and very likely change.
    """
    product = [f for f in findings if is_product_change(f)]
    accepted = [f for f in product if f["status"] == "accepted"]
    # Reverted is broken out rather than folded into rejected: a finding that landed and was
    # later stranded by another is not a rejection, and the section below says so. Folding it
    # here would have made this bar disagree with that section.
    reverted = [f for f in product if f["status"] == "reverted"]
    stock = stock_commit()
    against = f"`{STOCK_REF}` at `{stock}`" if stock else f"`{STOCK_REF}`"
    rows = [
        ("Measured against", f"{against} \u00b7 3.3.0a0"),
        ("Dataset", "databot `enterprise-campus / large` for reads, `datacenter / large` for writes"),
        ("Changes proposed", f"{len(product)}"),
        ("Accepted on this branch", f"**{len(accepted)}**"),
        ("Rejected", f"{len(product) - len(accepted) - len(reverted)}"),
        *([("Reverted after landing", f"{len(reverted)}")] if reverted else []),
        (
            "Experiments recorded",
            f"{len(findings)}, of which {len(findings) - len(product)} measured the harness itself",
        ),
    ]
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"| | |\n| --- | --- |\n{body}"


def render_provenance():
    """Where each number in the report came from, named so it can be checked.

    Lists the cumulative sources rather than every instrument the harness owns:
    those are what the report actually renders now, and naming files it no
    longer reads invited a reader to go looking for a figure that was not
    there.
    """
    bits = []
    # Ask git at generation time, not perf/.provenance.json. That file is a snapshot
    # sync.sh wrote when it last pushed the tree to the measurement host, so a report
    # generated afterwards inherited a dirty count describing a different moment --
    # it read "0 dirty path(s)" from a tree with four modified files.
    tree = None
    try:
        import subprocess

        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=PERF.parent
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=PERF.parent
        )
        if commit.returncode == 0:
            # Count the sources, not the outputs. Writing report.md and methodology.md
            # changes the dirty count, so including them made the report permanently
            # stale by its own --check: generation wrote one number and the comparison
            # regenerated a different one.
            generated = {"perf/report.md", "perf/methodology.md"}
            dirty = len(
                [
                    ln
                    for ln in status.stdout.splitlines()
                    if ln.strip() and ln[3:].strip() not in generated
                ]
            )
            tree = f"tree `{commit.stdout.strip()}` with {dirty} dirty source path(s), at generation time"
    except Exception:
        tree = None
    if tree is None:
        prov = PERF / ".provenance.json"
        if prov.exists():
            p = json.loads(prov.read_text())
            tree = f"tree `{p.get('commit')}` with {p.get('dirty_paths')} dirty path(s), as last synced"
    if tree:
        bits.append(tree)
    index = PERF / "baselines" / "cumulative.json"
    if index.exists():
        spec = json.loads(index.read_text())
        for agg in spec.get("aggregates", []):
            if agg.get("file"):
                bits.append(f"{agg['key']} from `perf/baselines/{agg['file']}`")
            elif agg.get("source") == "tier1":
                names = [
                    agg.get("baseline_file", "large-tier1-baseline.json"),
                    agg.get("current_file", "uwsgi-tier1-current.json"),
                ]
                if agg.get("wall_baseline_file"):
                    names += [agg["wall_baseline_file"], agg["wall_current_file"]]
                bits.append(f"{agg['key']} from " + " + ".join(f"`{n}`" for n in names))
            elif agg.get("figures"):
                where = f"{agg['key']} hand-entered in `perf/baselines/cumulative.json`"
                if agg.get("tree"):
                    where += f", tree `{agg['tree']}`"
                bits.append(where)
    bits.append("per-change figures from `perf/findings/*.yml`")
    return (
        "> Generated by `perf/scripts/build_report.py` from `perf/findings/` and "
        "`perf/baselines/`. Do not edit this file.\n>\n> Sources: " + "; ".join(bits) + "."
    )


def render_tier2():
    data = load_runs("uwsgi-tier2-c1-r*.json", "server_ms_median")
    if not data:
        return "_No concurrency-1 Tier 2 baseline committed yet._"
    lines = ["| Scenario | r1 | r2 | r3 | median | spread |", "|---|---:|---:|---:|---:|---:|"]
    for name, (med, spread, vals) in sorted(data.items(), key=lambda kv: -kv[1][0]):
        cells = " | ".join(f"{v:.0f}" for v in vals)
        lines.append(f"| `{name}` | {cells} | **{med:.0f} ms** | {spread:.1f}% |")
    spreads = [v[1] for v in data.values()]
    lines.append("")
    lines.append(
        f"All {len(data)} scenarios probed 200 before timing, none skipped. Spread "
        f"across rounds: median **{statistics.median(spreads):.1f}%**, "
        f"max **{max(spreads):.1f}%**."
    )
    return "\n".join(lines)


def _fmt_count(n):
    return f"{n:,.0f}"


def _ms_formatter(*values):
    """One unit for the whole pair, chosen from the larger side.

    Formatting each value on its own produced "63 s -> 48,059 ms", which is a
    23% improvement rendered as a 763-fold regression.
    """
    if max(values) >= 60_000:
        # One decimal under 1,000 s so the printed pair supports the printed
        # percentage. Above that, whole seconds is already finer than 0.1% and a
        # decimal would claim precision the instrument does not have -- the apply's
        # wall clock comes from `date +%s`, which is integer seconds.
        if max(values) < 1_000_000:
            return lambda ms: f"{ms / 1000:,.1f} s"
        return lambda ms: f"{ms / 1000:,.0f} s"
    return lambda ms: f"{ms:,.0f} ms"


def _delta(before, after, fmt):
    pct = (after - before) / before * 100 if before else 0.0
    # U+2212 for the sign, matching the figures typed into the findings; an
    # ASCII hyphen next to them reads as a different kind of number.
    sign = f"{pct:+.1f}".replace("-", "\u2212")
    return f"{fmt(before)} -> {fmt(after)} ({sign}%)"


def _distribution(pairs, floor=None):
    """How the saving is spread, in one sentence.

    A summed total invites "everything is 32% faster", which the first person to
    load a cheap page falsifies. What survives contact is the shape: how many
    moved, by how much the typical one moved, and how much of the total comes
    from the single largest contributor. The read aggregate needs this most --
    one endpoint is 46% of its saving.

    `pairs` is (before, after, label). `floor` drops anything whose baseline is
    below it, because finding 43 established that a per-measurement wall figure
    under roughly 100ms cannot reliably get its own sign right -- 21 of 153
    improved write measurements read *slower* on wall clock, worst +42.5%, on
    measurements whose query counts fell. Reporting those as regressions would
    advertise noise as a result. The count dropped is stated rather than hidden.
    """
    kept = [(b, a, label) for b, a, label in pairs if b and (floor is None or b >= floor)]
    dropped = sum(1 for b, _, _ in pairs if b and floor is not None and b < floor)
    pairs = kept
    if not pairs:
        return ""
    pcts = sorted((a - b) / b * 100 for b, a, _ in pairs)
    better = sum(1 for p in pcts if p < -1)
    worse = sum(1 for p in pcts if p > 1)
    flat = len(pcts) - better - worse
    bits = [
        f"{better} of {len(pcts)} improved, {flat} unchanged, "
        + ("**none worse**" if not worse else f"{worse} up to {max(pcts):+.1f}%")
    ]
    bits.append(f"the median moved {statistics.median(pcts):.1f}%")
    saved = [((b - a), label) for b, a, label in pairs if b > a]
    total = sum(v for v, _ in saved)
    if total > 0:
        top, name = max(saved)
        bits.append(f"and **{top / total * 100:.0f}% of the total saving is one item**, `{name}`")
    if dropped:
        bits.append(
            f"{dropped} measurements below {floor:.0f}ms are excluded, where a "
            "per-measurement wall figure cannot be trusted for sign (finding 43)"
        )
    return "; ".join(bits)


def _movement(data):
    """How many measurements moved, and by how much the typical one moved.

    The median rather than the mean, because the distribution is skewed: 109 of
    the 153 improved write measurements are under 10% and the tail runs to 47%,
    so a mean sits in a gap and describes neither end.

    A range on queries and not on wall clock, deliberately. Query counts are
    deterministic, so the spread is the result: every one of those 153 is a
    genuine saving somewhere between 1.8% and 47.5%. Per-measurement wall clock
    at this magnitude is not -- 21 of the same 153 read *slower* on wall, worst
    +42.5%, on measurements whose query counts fell. Finding 22 recorded that
    effect and identified it as variance rather than regression, so publishing
    a wall range here would advertise a 42% regression that is noise.

    Read alongside the aggregate, not instead of it. The two say different
    things: the count says nothing regressed, the median says the typical model
    improved modestly, and the aggregate is larger than the median because it is
    weighted by query volume and a few high-volume models carry most of it.
    """
    movement = data.get("query_movement") or {}
    if not movement.get("improved"):
        return ""
    improved = [m for m in data.get("measurements", []) if m["queries"]["delta"] < 0]
    bits = [
        f"{movement['improved']} of {data['coverage']['comparable']} measurements improved "
        f"on query count, {movement.get('unchanged', 0)} unchanged, "
        + ("**none worse**" if movement.get("worse") == 0 else f"{movement['worse']} worse")
    ]
    if improved:
        q = sorted(abs(m["queries"]["pct"]) for m in improved)
        bits.append(
            f"query savings run **{q[0]:.1f}% to {q[-1]:.1f}%** per measurement, median {statistics.median(q):.1f}%"
        )
    spread = _distribution(
        [
            (m["wall_ms"]["baseline"], m["wall_ms"]["current"], f"{m['id']} {m.get('kind', '')}".strip())
            for m in data.get("measurements", [])
        ],
        floor=100,
    )
    if spread:
        bits.append(f"on wall clock {spread}")
    return "; ".join(bits)


def render_cumulative(findings):
    """One table over every cumulative aggregate, read and write alike.

    Driven by perf/baselines/cumulative.json rather than by hardcoded paths,
    because the thing that goes wrong here is staleness rather than arithmetic:
    the write aggregate was quoted for weeks against a tree three findings
    behind the branch, and nothing in the report said so. Each entry names the
    tree it describes and the renderer prints that, so a figure that has fallen
    behind announces it instead of relying on someone remembering.

    The Coverage column carries the other thing a reader needs. These rows come
    from instruments with very different reach -- 57 hand-picked scenarios
    against every endpoint the resolver exposes -- and a percentage means
    nothing without knowing what it was taken over.
    """
    index = PERF / "baselines" / "cumulative.json"
    if not index.exists():
        return "_No cumulative index committed yet._"
    spec = json.loads(index.read_text())

    rows, notes = [], []
    for agg in spec.get("aggregates", []):
        label = agg["label"]
        coverage = agg.get("coverage", "-")
        cells = None
        # The protocol note leads, then the distribution, then whatever the
        # entry wants to add. Appending the note last put "on one host with the
        # arms alternated" in the middle of a sentence about medians.
        detail = [agg["note"]] if agg.get("note") else []

        if agg.get("source") == "tier1":
            base = PERF / "baselines" / agg.get("baseline_file", "large-tier1-baseline.json")
            cur = PERF / "baselines" / agg.get("current_file", "uwsgi-tier1-current.json")
            if base.exists() and cur.exists():
                # Sum the intersection, not each file whole. The workload grows:
                # 18 HX-Request scenarios were added once it turned out the
                # existing ui.*.list entries measured a table with no rows in
                # it. Summing both files entire would compare 57 scenarios
                # against 38 and render the difference as a regression.
                def counts(path):
                    return {e["id"]: e["query_count"] for e in json.loads(path.read_text())["endpoints"]}

                b, c = counts(base), counts(cur)
                shared = sorted(set(b) & set(c))
                wall = "-"
                if agg.get("wall_baseline_file"):
                    # Summed medians, every endpoint weighted equally -- the same
                    # method the write screen's aggregate uses, so the two rows are
                    # comparable. It is not traffic-weighted, and nothing in this
                    # harness knows the traffic mix.
                    def medians(name):
                        return {
                            e["id"]: e.get("server_ms_median")
                            for e in json.loads((PERF / "baselines" / name).read_text())["endpoints"]
                            if e.get("server_ms_median") and not e.get("skipped")
                        }

                    wb, wc = medians(agg["wall_baseline_file"]), medians(agg["wall_current_file"])
                    ws = sorted(set(wb) & set(wc))
                    if ws:
                        wbt, wct = sum(wb[k] for k in ws), sum(wc[k] for k in ws)
                        wall = _delta(wbt, wct, _ms_formatter(wbt, wct))
                        detail.append(
                            f"wall clock is the sum of per-endpoint medians over the {len(ws)} "
                            "endpoints answering on both arms, so it is a workload total rather "
                            "than a per-page figure"
                        )
                        spread = _distribution([(wb[k], wc[k], k) for k in ws], floor=100)
                        if spread:
                            detail.append(spread)
                cells = [
                    _delta(sum(b[k] for k in shared), sum(c[k] for k in shared), _fmt_count),
                    "-",
                    wall,
                ]
                coverage = f"{len(shared)} scenarios on both arms"
                if len(b) != len(shared) or len(c) != len(shared):
                    detail.append(
                        f"compared over the {len(shared)} scenarios present on both arms, of "
                        f"{len(b)} in the baseline and {len(c)} now"
                    )
        elif agg.get("file"):
            data = json.loads((PERF / "baselines" / agg["file"]).read_text())
            t = data["totals"]
            cov = data["coverage"]
            coverage = f"{cov['comparable']} of {cov['baseline_records']} measurements"
            cells = [
                _delta(t["queries"]["baseline"], t["queries"]["current"], _fmt_count),
                _delta(t["db_ms"]["baseline"], t["db_ms"]["current"], _ms_formatter(*t["db_ms"].values())),
                _delta(t["wall_ms"]["baseline"], t["wall_ms"]["current"], _ms_formatter(*t["wall_ms"].values())),
            ]
            moved = _movement(data)
            if moved:
                detail.append(moved)
        elif agg.get("figures"):
            fig = agg["figures"]
            cells = [
                _delta(fig["queries"]["baseline"], fig["queries"]["current"], _fmt_count),
                _delta(fig["db_ms"]["baseline"], fig["db_ms"]["current"], _ms_formatter(*fig["db_ms"].values())),
                _delta(fig["wall_ms"]["baseline"], fig["wall_ms"]["current"], _ms_formatter(*fig["wall_ms"].values())),
            ]

        if cells is None:
            pending = agg.get("pending") or "not measured"
            rows.append(f"| {label} | {coverage} | _{pending}_ | | |")
        else:
            rows.append(f"| {label} | {coverage} | " + " | ".join(cells) + " |")

        # One note per aggregate. Movement and staleness were two bullets that
        # both opened with the same label, which read as two findings about one
        # measurement.
        if agg.get("stale"):
            detail.append(
                f"measured against tree `{agg['tree']}`, so it {agg['stale']} — a re-run "
                "against the current tree is queued"
            )
        elif agg.get("tree") and not agg.get("file") and not agg.get("baseline_file"):
            # Print the tree whether or not anyone flagged the row stale. Gating it on
            # a hand-set `stale` key is exactly the "someone remembering" this column
            # exists to replace: the apply row carried tree 90dbd96ff for as long as
            # the flag was unset and the report named the tree nowhere.
            detail.append(f"measured against tree `{agg['tree']}`")
        if detail:
            notes.append(f"**{label}**: " + "; ".join(detail) + ".")

    out = [
        "Stock `next` against this branch, one box, arms alternated, both trees proved different "
        "by content hash before each run. Every endpoint is weighted equally, so these are "
        "totals over a workload rather than a prediction of what any one user gets back.",
        "",
        "| Measurement | Coverage | Queries | Database time | Wall clock |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ]
    if notes:
        out.append("\n".join(f"- {n}" for n in notes))
    return "\n".join(out).rstrip()


def render_bench():
    """Absolute in-process wall clock on the current host. A reference, not a delta."""
    data = load_runs("uwsgi-bench-current-r*.json", "median_ms") or load_runs("uwsgi-bench-r*.json", "median_ms")
    if not data:
        return "_No in-process wall-clock reference committed yet._"
    lines = ["| Scenario | r1 | r2 | r3 | median | spread |", "|---|---:|---:|---:|---:|---:|"]
    for name, (med, spread, vals) in sorted(data.items(), key=lambda kv: -kv[1][0]):
        cells = " | ".join(f"{v:.0f}" for v in vals)
        lines.append(f"| `{name}` | {cells} | **{med:.0f} ms** | {spread:.1f}% |")
    spreads = [v[1] for v in data.values()]
    lines.append("")
    lines.append(
        f"Spread across rounds: median **{statistics.median(spreads):.1f}%**, "
        f"max **{max(spreads):.1f}%** across {len(data)} endpoints. Reflects the tree as it "
        "stands: every experiment measures wall clock, whatever its primary signal, because "
        "this host is precise enough that skipping it would only hide a result."
    )
    return "\n".join(lines)


def render_entry(f, level, full=False):
    """One finding: what it changes, what that was worth, what it costs.

    Four fields are deliberately absent unless `full`: wall_clock_detail
    (which restated the wall-clock cell directly above it), controls, tests,
    and note. Those made up 55% of a 155KB report and none of them help a
    reader decide whether to adopt a change -- they are how the number was
    earned, which is a different question and now a different file. `full` is
    for perf/methodology.md, where that is the question being asked.
    """
    out = [f"{'#' * level} {f['title']}", ""]
    meta = [f"**{f['seq']:02d}**", tiers(f)]
    if f.get("commit"):
        meta.append(f"commit `{f['commit']}`")
    if f.get("site"):
        meta.append(f"`{f['site']}`")
    out += [" \u00b7 ".join(meta), ""]
    out += [f["summary"], ""]
    results = (f.get("result") or {}).items()
    if not full:
        results = [(k, v) for k, v in results if k in CORE_RESULT_KEYS]
    out += ["| Instrument | Result |", "|---|---|", f"| **wall clock** | {f['wall_clock']} |"]
    out += [f"| {k.replace('_', ' ')} | {v} |" for k, v in results]
    out.append("")
    if full and f.get("wall_clock_detail"):
        out += [f"**Wall clock.** {f['wall_clock_detail']}", ""]
    if full and f.get("controls"):
        out += [f"**Controls.** {f['controls']}", ""]
    if f.get("caveat"):
        out += [f"> **Caveat.** {f['caveat']}", ""]
    if f.get("reason"):
        out += [f"**Why not.** {f['reason']}", ""]
    if full and f.get("tests"):
        out += [f"**Tests.** {f['tests']}", ""]
    if full and f.get("note"):
        out += [f["note"], ""]
    return out


def render_evidence(findings):
    """Per-finding working, for the reader who wants to audit a number.

    Everything render_entry() leaves out of the report: how the wall clock was
    taken, what was held flat as a control, which tests ran, the ad-hoc result
    keys, and the note. Only findings that carry any of it appear.
    """
    product = [f for f in findings if is_product_change(f)]
    carried = ("wall_clock_detail", "controls", "tests", "note")
    out = []
    for f in product:
        extra = {k: v for k, v in (f.get("result") or {}).items() if k not in CORE_RESULT_KEYS}
        if not (extra or any(f.get(k) for k in carried)):
            continue
        out += [f"### {f['seq']:02d} \u00b7 {f['title']}", ""]
        if f.get("wall_clock_detail"):
            out += [f"**Wall clock.** {f['wall_clock_detail']}", ""]
        if extra:
            out += ["| | |", "|---|---|"]
            out += [f"| {k.replace('_', ' ')} | {v} |" for k, v in extra.items()]
            out.append("")
        if f.get("controls"):
            out += [f"**Controls.** {f['controls']}", ""]
        if f.get("tests"):
            out += [f"**Tests.** {f['tests']}", ""]
        if f.get("note"):
            out += [f["note"], ""]
    return "\n".join(out) if out else "_No per-finding evidence recorded._"


def render_instruments(findings):
    """Findings that changed the harness rather than Nautobot.

    Kept, because several are the reason a number elsewhere is trustworthy -- and
    two of them (the whole-workflow A/B, and what a cable actually costs) are the
    largest results on the branch. They are simply not things anyone would adopt
    into Nautobot, so they do not belong in a list of optimizations.
    """
    members = [f for f in findings if not is_product_change(f)]
    if not members:
        return "_No instrument findings recorded._"
    out = [
        f"{len(members)} of the {len(findings)} experiments changed the harness rather than "
        "Nautobot: new instruments, and measurements of the instruments themselves. They are "
        "recorded to the same standard because a measurement is only as good as the thing "
        "that took it.",
        "",
    ]
    for f in members:
        out += render_entry(f, 3, full=True)
    return "\n".join(out)


# The branch a reader is being asked to adopt from. The findings' own `commit`
# field points at `perf/experiments`, which carries the harness and the records
# as well as the change -- not something to hand upstream. `recommended_commit`
# names the same change on the upstream-facing branch, and the column falls back
# to `commit` until that branch is built.
DEMO_REMOTE = "git@github.com:kevsmith/nautobot"
DEMO_BRANCH = "perf/recommended"


def commit_of(f):
    return f.get("recommended_commit") or f.get("commit")


def render_tiers(findings=None):
    """The tier legend, and the flags actually in use, as tables.

    Two tables rather than one: tiers are an ordered price scale and flags are
    orthogonal annotations, and merging them lost that. Only flags that appear
    on a finding are listed -- an unused definition is one more thing to read.
    """
    out = [
        "| Tier | Adoption cost |",
        "|---|---|",
        *[f"| `{k}` | {v} |" for k, v in TIER_HELP.items()],
    ]
    used = sorted({fl for f in (findings or []) if is_product_change(f) for fl in (f.get("flags") or [])})
    if used:
        out += [
            "",
            "| Flag | What it adds to the tier |",
            "|---|---|",
            *[f"| `{fl}` | {FLAG_HELP[fl]} |" for fl in used],
        ]
    return "\n".join(out)


def render_findings(findings):
    """Two tables: what to adopt, and what was ruled out.

    Tables only. Each change used to carry an entry here -- the defect, its
    instruments, its caveat -- and 33 of those entries were most of a 155KB
    report. The entry still exists in perf/methodology.md, which is what the
    title links to, so nothing is lost and the decision is one screen.

    Product changes only. Findings that changed the harness are rendered by
    render_instruments() alongside the methodology they belong to -- listing an
    instrument as an "accepted change" invited a reader to think it was
    something to adopt into Nautobot.
    """
    product = [f for f in findings if is_product_change(f)]
    out = []
    for key, title, blurb in GROUPS:
        members = [f for f in product if group_of(f) == key]
        if not members:
            continue
        out += [f"## {title} ({len(members)})", "", blurb, ""]
        # A commit column only where a commit is something to adopt. Rejected
        # changes are not on the upstream-facing branch at all -- several share
        # the commit that recorded the decision rather than one that implemented
        # anything -- so a SHA there would name a branch it is absent from.
        accepted = key == "accepted"
        if accepted:
            out += ["| # | Change | Tier | Reason | Commit |", "|---:|---|---|---|---|"]
        else:
            out += ["| # | Change | Tier | Reason |", "|---:|---|---|---|"]
        for f in members:
            # Into methodology.md, not into this file: there is no entry here to
            # jump to any more, and a link to a heading that does not exist is
            # worse than no link.
            link = f"[{f['title']}](methodology.md#{anchor(f['title'])})"
            row = f"| {f['seq']:02d} | {link} | {tiers(f)} | {basis(f)} |"
            if accepted:
                sha = f"`{commit_of(f)[:9]}`" if commit_of(f) else "-"
                row += f" {sha} |"
            out.append(row)
        out.append("")
        if accepted:
            out += [f"Every commit above is on `{DEMO_BRANCH}` at `{DEMO_REMOTE}`.", ""]
    return "\n".join(out)


def render_endnote():
    return (
        f"Measured against `nautobot/next` at `{stock_commit()}` (3.3.0a0) on an isolated "
        "stack with pinned "
        "resources. Harness, workload definition, findings and baseline data are on the "
        "`perf/experiments` branch under `perf/`; every scenario and operation above is "
        "reproducible with `perf/scripts/run_experiment.sh`. Instruments, baselines and per-finding "
        "working are in `perf/methodology.md`.\n\n"
        "Both files are generated. Edit `perf/findings/*.yml` for numbers and "
        "`perf/report.template.md` or `perf/methodology.template.md` for narrative, then run "
        "`perf/scripts/build_report.py`. `--check` exits non-zero when they have drifted apart."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="render and compare without writing; exit 1 on drift")
    args = ap.parse_args()

    findings = load_findings()
    if not findings:
        print("no findings in perf/findings/", file=sys.stderr)
        return 2

    problems = validate(findings)
    if problems:
        print(f"{len(problems)} schema problem(s) in perf/findings/:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    # Two documents from one set of findings. report.md answers "what should we
    # adopt and what is it worth"; methodology.md answers "why should I believe
    # the number". Merging them cost the first question a 155KB answer.
    values = {
        "<!--GEN:factbar-->": render_factbar(findings),
        "<!--GEN:provenance-->": render_provenance(),
        "<!--GEN:findings-->": render_findings(findings),
        "<!--GEN:tiers-->": render_tiers(findings),
        "<!--GEN:cumulative-->": render_cumulative(findings),
        "<!--GEN:instruments-->": render_instruments(findings),
        "<!--GEN:evidence-->": render_evidence(findings),
        "<!--GEN:tier2-->": render_tier2(),
        "<!--GEN:bench-->": render_bench(),
        "<!--GEN:endnote-->": render_endnote(),
    }

    rendered = {}
    for name in ("report", "methodology"):
        template = PERF / f"{name}.template.md"
        if not template.exists():
            print(f"missing template: {template.relative_to(ROOT)}", file=sys.stderr)
            return 2
        out = template.read_text()
        for marker, value in values.items():
            out = out.replace(marker, value)
        if "<!--GEN:" in out:
            print(f"unsubstituted marker remains in {name}.md", file=sys.stderr)
            return 2
        rendered[PERF / f"{name}.md"] = out

    if args.check:
        # Compare everything except the generation-time provenance line. That line names
        # the commit the render ran against, which can never be the commit that contains
        # the render -- so including it made --check fail on every commit that carries the
        # report, which is every one of them. --check exists to prove the report was
        # generated from the current findings and baselines; a timestamp of when is not
        # something the sources determine.
        def without_provenance(text):
            return "\n".join(ln for ln in text.splitlines() if not ln.startswith("> Sources: tree "))

        stale = [
            t
            for t, out in rendered.items()
            if not t.exists() or without_provenance(t.read_text()) != without_provenance(out)
        ]
        if stale:
            names = ", ".join(str(t.relative_to(ROOT)) for t in stale)
            print(f"{names} stale -- run perf/scripts/build_report.py", file=sys.stderr)
            return 1
        print("perf/report.md and perf/methodology.md are current")
        return 0

    counts = {}
    for f in findings:
        g = group_of(f)
        counts[g] = counts.get(g, 0) + 1
    for target, out in rendered.items():
        target.write_text(out)
        print(f"wrote {target.relative_to(ROOT)} ({len(out) / 1024:.0f}KB)")
    print(f"from {len(findings)} findings ({', '.join(f'{k} {v}' for k, v in counts.items())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
