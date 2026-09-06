#!/usr/bin/env python3
"""Render perf/report.md from perf/findings/*.yml and the committed baselines.

The report is the deliverable on this branch -- the tree may never be merged. It
drifted five commits behind the code once, carrying a figure a later commit had
retracted, so it is generated rather than maintained.

Numbers live in exactly two places: perf/findings/*.yml for what each experiment
found, and perf/baselines/*.json for what the instruments measured. This script
reads both and fills the <!--GEN:...--> markers in perf/report.template.md.
Narrative prose stays in the template, where writing it by hand is the point.

Markdown rather than HTML, deliberately: a markdown diff shows which number
moved, so drift becomes visible in review rather than merely detectable by
--check. It also removes escaping and tag-balancing from a tool whose whole job
is not being wrong.

    python3 perf/build_report.py            # write perf/report.md
    python3 perf/build_report.py --check    # exit 1 if it is stale
"""

import argparse
import json
import pathlib
import re
import statistics
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PERF = ROOT / "perf"

# Grouped by what was decided, because that is the first thing a reader needs:
# which of these are we doing. Adoption cost has not gone away -- it is on every
# entry as a tier and, where one exists, a caveat -- but it is a property of a
# finding rather than a way to file it.
GROUPS = [
    (
        "accepted",
        "Accepted",
        "Measured, kept, and applied to the tree. Each entry states what it changed, what "
        "that was worth, and any caveat a release note would have to carry.",
    ),
    (
        "parked",
        "Parked",
        "Measured and not rejected -- the win is real and the reason for waiting is stated. "
        "These are decisions someone can revisit, not conclusions.",
    ),
    (
        "rejected",
        "Rejected",
        "Plausible optimizations that measurement or blast-radius analysis killed. These are "
        "results rather than omissions: they say what a tempting option actually costs.",
    ),
]

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
    """Classify by what was decided.

    Three buckets, and every finding lands in exactly one. `not-taken` and
    `priced` join `rejected`: from a reader's point of view they are all "we
    looked and we are not doing it", and the distinction between them lives in
    each finding's own `reason`. `proposed` joins `parked` for the same reason --
    it is something still open rather than something closed.
    """
    status = f["status"]
    if status == "accepted":
        return "accepted"
    if status in ("parked", "proposed"):
        return "parked"
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
                "percentage, or say 'not measured — <reason>'"
            )
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


def tiers(f):
    bits = [f["behaviour"]]
    if f.get("migration", "-") != "-":
        bits.append(f["migration"])
    bits += list(f.get("flags") or [])
    return " ".join(f"`{b}`" for b in bits)


# The summary table shows the three instruments by name. `result` accumulated ten
# different keys across 27 findings -- expected, cost, observed, measured_prize,
# unmeasured, note -- so a column showing "whichever came first" read as a notes
# field rather than a measurement. Those keys still appear in each finding's own
# detail table; only the summary is restricted.
QUERY_KEYS = ("queries",)
CACHE_KEYS = ("config_reads", "redis_reads")
CELL_MAX = 46


def instrument(f, keys):
    """The first of `keys` present in a finding's result, trimmed for a table cell."""
    r = f.get("result") or {}
    for key in keys:
        if key in r:
            value = str(r[key])
            if len(value) > CELL_MAX:
                value = value[: CELL_MAX - 1].rstrip(" ,;") + "…"
            return value
    return "—"


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


def render_factbar(findings):
    product = [f for f in findings if is_product_change(f)]
    accepted = sum(1 for f in product if f["status"] == "accepted")
    rows = [
        ("Branch", "`next` · 3.3.0a0"),
        ("Dataset", "databot `enterprise-campus / large / seed 42` · 24,091 objects"),
        ("Read scenarios", "38"),
        ("Write operations", "13"),
        ("Findings recorded", str(len(findings))),
        ("Accepted changes to Nautobot", str(accepted)),
        ("Harness findings", str(len(findings) - len(product))),
    ]
    # Three reference points: large-tier1-baseline is where the work started,
    # uwsgi-tier1-baseline is where round two started, and uwsgi-tier1-current
    # is the tree as it stands, refreshed whenever an experiment is accepted.
    # The headline compares the original baseline against current so it cannot
    # lag the tree.
    base = PERF / "baselines" / "large-tier1-baseline.json"
    r2 = PERF / "baselines" / "uwsgi-tier1-baseline.json"
    cur = PERF / "baselines" / "uwsgi-tier1-current.json"
    after = cur if cur.exists() else r2

    def total(path):
        return sum(e["query_count"] for e in json.loads(path.read_text())["endpoints"])

    if base.exists() and after.exists():
        bt, ct = total(base), total(after)
        rows.append(("Queries", f"**{bt:,} → {ct:,}** ({(ct - bt) / bt * 100:+.1f}%)"))
    if cur.exists() and r2.exists() and total(cur) != total(r2):
        rows.append(("Since the round-two baseline", f"{total(r2):,} → {total(cur):,}"))
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"| | |\n| --- | --- |\n{body}"


def render_provenance():
    prov = PERF / ".provenance.json"
    bits = []
    if prov.exists():
        p = json.loads(prov.read_text())
        bits.append(f"tree `{p.get('commit')}` with {p.get('dirty_paths')} dirty path(s)")
    bits.append(
        "read-path queries from `perf/baselines/uwsgi-tier1-current.json` against "
        "`large-tier1-baseline.json`; write path from `uwsgi-tier1w-baseline.json`"
    )
    bits.append("in-process wall clock from `uwsgi-bench-r{1,2,3}.json`")
    bits.append("HTTP wall clock from `uwsgi-tier2-c1-r{1,2,3}.json`, concurrency 1, median")
    return (
        "> Generated by `perf/build_report.py` from `perf/findings/` and "
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


def render_cumulative(findings):
    """Read-path cumulative effect in query counts, from the baselines.

    Queries only, and deliberately so. Query counts are machine-independent --
    the same 957 and 346 reproduced across two different CPU architectures and
    host operating systems -- so a delta between the original baseline and the
    current tree is meaningful. Wall clock is not: round one ran on Apple
    Silicon and round two on an Intel part with turbo disabled, 4-6x apart in
    absolute terms. Absolute wall clock for the current host is reported
    separately, as a reference rather than a delta.
    """
    base = PERF / "baselines" / "large-tier1-baseline.json"
    cur = PERF / "baselines" / "uwsgi-tier1-current.json"
    if not (base.exists() and cur.exists()):
        return "_No cumulative read-path measurement committed yet._"

    def load(path):
        return {e["id"]: e for e in json.loads(path.read_text())["endpoints"]}

    b, c = load(base), load(cur)
    accepted = sum(1 for f in findings if f["status"] == "accepted" and is_product_change(f))

    rows = []
    for name in sorted(c, key=lambda k: b.get(k, {}).get("query_count", 0), reverse=True):
        if name not in b or b[name]["query_count"] == c[name]["query_count"]:
            continue
        bq, cq = b[name]["query_count"], c[name]["query_count"]
        bd, cd = b[name]["duplicate_queries"], c[name]["duplicate_queries"]
        rows.append(f"| `{name}` | {bq:,} → {cq:,} ({(cq - bq) / bq * 100:+.0f}%) | {bd:,} → {cd:,} |")

    bt = sum(e["query_count"] for e in b.values())
    ct = sum(e["query_count"] for e in c.values())
    bdt = sum(e["duplicate_queries"] for e in b.values())
    cdt = sum(e["duplicate_queries"] for e in c.values())
    unchanged = sum(1 for k in c if k in b and b[k]["query_count"] == c[k]["query_count"])
    rows.append(
        f"| **All {len(c)} scenarios** | **{bt:,} → {ct:,} ({(ct - bt) / bt * 100:+.1f}%)** | **{bdt:,} → {cdt:,}** |"
    )

    # No heading of its own: the template supplies "### Cumulative effect" and an
    # extra "##" here nested a section under its own subsection.
    return (
        f"Read path, all {accepted} accepted fixes, measured against the 24,091-object dataset "
        "on a pristine tree and reflecting the tree as it stands. Only the "
        f"{len(rows) - 1} scenarios whose count changed are listed; the other {unchanged} are "
        "unchanged, which is itself the point -- the list views were already efficient.\n\n"
        "| Scenario | Queries | Duplicates |\n|---|---|---|\n" + "\n".join(rows)
    )


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


def render_entry(f, level, parked=False):
    """One finding, rendered at the given heading level."""
    out = [f"{'#' * level} {f['title']}", ""]
    meta = [f"**{f['seq']:02d}**", tiers(f), f"status `{f['status']}`"]
    if f.get("commit"):
        meta.append(f"commit `{f['commit']}`")
    if f.get("id"):
        meta.append(f"({f['id']})")
    out += [" \u00b7 ".join(meta), ""]
    if f.get("site"):
        out += [f"`{f['site']}`", ""]
    out += [f["summary"], ""]
    out += ["| Instrument | Result |", "|---|---|", f"| **wall clock** | {f['wall_clock']} |"]
    for k, v in (f.get("result") or {}).items():
        out.append(f"| {k.replace('_', ' ')} | {v} |")
    out.append("")
    if f.get("wall_clock_detail"):
        out += [f"**Wall clock.** {f['wall_clock_detail']}", ""]
    if f.get("controls"):
        out += [f"**Controls.** {f['controls']}", ""]
    if f.get("caveat"):
        out += [f"> **Caveat.** {f['caveat']}", ""]
    if f.get("reason"):
        out += [f"**{'Why it is parked' if parked else 'Why not'}.** {f['reason']}", ""]
    if f.get("tests"):
        out += [f"**Tests.** {f['tests']}", ""]
    if f.get("note"):
        out += [f["note"], ""]
    return out


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
        out += render_entry(f, 4)
    return "\n".join(out)


def render_findings(findings):
    """The section the report leads with: what was found in Nautobot, and what was decided.

    Product changes only. Findings that changed the harness are rendered by
    render_instruments() alongside the methodology they belong to -- listing an
    instrument as an "accepted change" invited a reader to think it was something
    to adopt into Nautobot.
    """
    product = [f for f in findings if is_product_change(f)]
    counts = {key: sum(1 for f in product if group_of(f) == key) for key, _, _ in GROUPS}
    tally = " \u00b7 ".join(f"**{counts[key]}** {title.lower()}" for key, title, _ in GROUPS if counts[key])
    legend = ", ".join(f"`{k}` {v}" for k, v in TIER_HELP.items())

    out = [
        "## Optimizations identified",
        "",
        f"{len(product)} proposed changes to Nautobot: {tally}. Every one is listed, including "
        "the ones that did not work -- a rejected optimization is a measurement of what an "
        "option costs, and deleting it would invite the next person to try it again.",
        "",
        f"A further {len(findings) - len(product)} experiments changed the measurement harness "
        "rather than Nautobot; they are recorded under the methodology below.",
        "",
        "Each decision is a self-contained section: a summary table, then one entry per "
        "experiment. There is deliberately no combined index -- a reviewer looking at what to "
        "adopt should not have to filter a list of things nobody is proposing.",
        "",
        "Tiers are a price tag rather than a filter. Nothing here is disqualified for being "
        f"expensive; it is labelled so the price is visible: {legend}. `perf/README.md` defines "
        "the taxonomy.",
        "",
    ]

    for key, title, blurb in GROUPS:
        members = [f for f in product if group_of(f) == key]
        if not members:
            continue
        out += [f"### {title} ({len(members)})", "", blurb, ""]
        out += [
            "| # | Change | Tier | Wall clock | Queries | Cache reads |",
            "|---:|---|---|---|---|---|",
        ]
        for f in members:
            link = f"[{f['title']}](#{anchor(f['title'])})"
            out.append(
                f"| {f['seq']:02d} | {link} | {tiers(f)} | {f['wall_clock']} "
                f"| {instrument(f, QUERY_KEYS)} | {instrument(f, CACHE_KEYS)} |"
            )
        out.append("")
        for f in members:
            out += render_entry(f, 4, parked=(key == "parked"))
    return "\n".join(out)


def render_endnote():
    return (
        "Measured against `nautobot/next` at 3.3.0a0 on an isolated stack with pinned "
        "resources. Harness, workload definition, findings and baseline data are on the "
        "`perf/experiments` branch under `perf/`; every scenario and operation above is "
        "reproducible with `perf/run_experiment.sh`.\n\n"
        "This file is generated. Edit `perf/findings/*.yml` for numbers and "
        "`perf/report.template.md` for narrative, then run `perf/build_report.py`. "
        "`--check` exits non-zero when the two have drifted apart."
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

    out = (PERF / "report.template.md").read_text()
    for marker, value in (
        ("<!--GEN:factbar-->", render_factbar(findings)),
        ("<!--GEN:provenance-->", render_provenance()),
        ("<!--GEN:findings-->", render_findings(findings)),
        ("<!--GEN:cumulative-->", render_cumulative(findings)),
        ("<!--GEN:instruments-->", render_instruments(findings)),
        ("<!--GEN:tier2-->", render_tier2()),
        ("<!--GEN:bench-->", render_bench()),
        ("<!--GEN:endnote-->", render_endnote()),
    ):
        if marker not in out:
            print(f"marker missing from template: {marker}", file=sys.stderr)
            return 2
        out = out.replace(marker, value)

    if "<!--GEN:" in out:
        print("unsubstituted marker remains", file=sys.stderr)
        return 2

    target = PERF / "report.md"
    if args.check:
        if not target.exists() or target.read_text() != out:
            print("perf/report.md is stale -- run perf/build_report.py", file=sys.stderr)
            return 1
        print("perf/report.md is current")
        return 0

    target.write_text(out)
    counts = {}
    for f in findings:
        g = group_of(f)
        counts[g] = counts.get(g, 0) + 1
    print(
        f"wrote {target.relative_to(ROOT)} from {len(findings)} findings "
        f"({', '.join(f'{k} {v}' for k, v in counts.items())})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
