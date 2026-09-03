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

# Grouped by what it would cost to adopt, most-adoptable first. Deliberately not
# "shipped / not shipped", which is a distinction about us rather than about the
# finding.
GROUPS = [
    ("free", "Free wins", "No migration, no user-visible change, nothing to weigh. Adoptable as-is."),
    (
        "caveat",
        "Wins with a user-visible caveat",
        "Each one works and is measured. Whether the caveat is acceptable is a product "
        "decision rather than a measurement one, so it is quoted as a release note would "
        "have to write it.",
    ),
    (
        "operational",
        "Wins with an operational cost",
        "These need a migration. The cost of running it is stated, because it lands on "
        "operators rather than on the release.",
    ),
    (
        "proposed",
        "Proposed, not yet measured",
        "Targets with a mechanism and an expected effect, but no measurement behind them "
        "yet. Listed so they are not rediscovered, and so the expectation is on record to "
        "be falsified.",
    ),
    (
        "rejected",
        "Measured, and not worth it",
        "Plausible optimizations that measurement or blast-radius analysis killed. These are "
        "results, not omissions: they say what a tempting option actually costs.",
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
    """Classify by adoption cost.

    Order matters. A finding that was measured and not taken belongs with the
    rejections whatever tier it would otherwise carry, and a proposal is not a
    win -- grouping proposals under "free wins" claimed results that do not
    exist yet.
    """
    if f["status"] in ("rejected", "not-taken"):
        return "rejected"
    if f["status"] == "proposed":
        return "proposed"
    if f.get("migration", "-") != "-":
        return "operational"
    if f.get("caveat"):
        return "caveat"
    return "free"


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
WALL_OK = re.compile(r"^(?:[+−-]|≈[+−-]?)\d|^not measured|^not applicable|^no measurable")


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
        needs = f.get("behaviour") in ("B2", "C") or f.get("migration", "-") != "-"
        if needs and not f.get("caveat") and f.get("status") != "rejected":
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
    accepted = sum(1 for f in findings if f["status"] == "accepted")
    rows = [
        ("Branch", "`next` · 3.3.0a0"),
        ("Dataset", "databot `enterprise-campus / large / seed 42` · 24,091 objects"),
        ("Read scenarios", "38"),
        ("Write operations", "13"),
        ("Findings recorded", str(len(findings))),
        ("Accepted", str(accepted)),
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
    accepted = sum(1 for f in findings if f["status"] == "accepted")

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

    return (
        f"## Current state — all {accepted} accepted fixes\n\n"
        "Measured against the 24,091-object dataset on a pristine tree, and reflecting the "
        f"tree as it stands. Only the {len(rows) - 1} scenarios whose count changed are "
        f"listed; the other {unchanged} are unchanged, which is itself the point -- the "
        "list views were already efficient.\n\n"
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


def render_findings(findings):
    out = [
        "## Findings",
        "",
        "Every experiment, in order. The tier columns are a price tag rather than a "
        "filter -- nothing here is disqualified for being expensive, it is labelled so "
        "the price is visible. `perf/README.md` defines the taxonomy.",
        "",
        "| # | Change | Tier | Wall clock | Queries | Cache reads | Status |",
        "|---:|---|---|---|---|---|---|",
    ]
    for f in findings:
        link = f"[{f['title']}](#{anchor(f['title'])})"
        out.append(
            f"| {f['seq']:02d} | {link} | {tiers(f)} | {f['wall_clock']} "
            f"| {instrument(f, QUERY_KEYS)} | {instrument(f, CACHE_KEYS)} "
            f"| {f['status']} |"
        )
    out.append("")

    legend = ", ".join(f"`{k}` {v}" for k, v in TIER_HELP.items())
    out.append(f"Tiers: {legend}.")
    out.append("")

    for key, title, blurb in GROUPS:
        members = [f for f in findings if group_of(f) == key]
        if not members:
            continue
        out += [f"## {title}", "", blurb, ""]
        for f in members:
            out.append(f"### {f['title']}")
            out.append("")
            meta = [f"**{f['seq']:02d}**", tiers(f), f"status `{f['status']}`"]
            if f.get("commit"):
                meta.append(f"commit `{f['commit']}`")
            if f.get("id"):
                meta.append(f"({f['id']})")
            out.append(" · ".join(meta))
            out.append("")
            if f.get("site"):
                out += [f"`{f['site']}`", ""]
            out += [f["summary"], ""]
            r = f.get("result") or {}
            out.append("| Instrument | Result |")
            out.append("|---|---|")
            out.append(f"| **wall clock** | {f['wall_clock']} |")
            for k, v in r.items():
                out.append(f"| {k.replace('_', ' ')} | {v} |")
            out.append("")
            if f.get("wall_clock_detail"):
                out += [f"**Wall clock.** {f['wall_clock_detail']}", ""]
            if f.get("controls"):
                out += [f"**Controls.** {f['controls']}", ""]
            if f.get("caveat"):
                out += [f"> **Caveat.** {f['caveat']}", ""]
            if f.get("reason"):
                out += [f"**Why not.** {f['reason']}", ""]
            if f.get("tests"):
                out += [f"**Tests.** {f['tests']}", ""]
            if f.get("note"):
                out += [f["note"], ""]
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
