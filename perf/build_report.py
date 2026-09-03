#!/usr/bin/env python3
"""Render perf/report.html from perf/findings/*.yml and the committed results.

The report is the deliverable on this branch -- the tree may never be merged.
It has already drifted five commits behind the code once, carrying a figure a
later commit had retracted, so it is generated rather than maintained.

Numbers live in exactly two places: perf/findings/*.yml for what each experiment
found, and the result JSON for what the instruments measured. This script reads
both and fills the <!--GEN:...--> regions of perf/report.template.html. Narrative
prose stays in the template, where hand-writing it is the point.

    python3 perf/build_report.py [--check]

--check renders and compares without writing, exiting 1 on any difference. Use
it to detect drift rather than to fix it.
"""

import argparse
import html
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PERF = ROOT / "perf"

# Groups a reader actually wants: what would it cost me to adopt this. Ordered
# most-adoptable first. Deliberately not "shipped / not shipped", which is a
# distinction about us rather than about the finding.
GROUPS = [
    ("free", "Free wins",
     "No migration, no user-visible change, no caveat to weigh. Adoptable as-is."),
    ("caveat", "Wins with a user-visible caveat",
     "Each one works and is measured. Whether it is acceptable is a product "
     "decision, not a measurement one, so the caveat is quoted as a release "
     "note would have to write it."),
    ("operational", "Wins with an operational cost",
     "These need a migration. The cost of running it is stated, because it "
     "lands on operators rather than on the release."),
    ("rejected", "Measured, and not worth it",
     "Plausible optimizations that measurement or blast-radius analysis killed. "
     "These are results, not omissions -- they tell you what a tempting option "
     "actually costs."),
]

TIER_HELP = {
    "A": "no observable change",
    "B1": "state scoped to a request, transaction or instance",
    "B2": "state that outlives its scope and needs invalidating",
    "C": "changes observable behaviour",
    "-": "no migration",
    "M0": "metadata-only migration",
    "M1": "bounded single-pass migration",
    "M2": "unbounded migration",
}


def esc(text):
    return html.escape(str(text), quote=False)


def group_of(f):
    """Classify by adoption cost. Order matters: a rejected finding is rejected
    regardless of what tier it would otherwise have carried."""
    if f["status"] == "rejected":
        return "rejected"
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
    return out


def tier_tags(f):
    bits = [f'<span class="tag" title="{esc(TIER_HELP.get(f["behaviour"], ""))}">'
            f'{esc(f["behaviour"])}</span>']
    if f.get("migration", "-") != "-":
        bits.append(f'<span class="tag" title="{esc(TIER_HELP.get(f["migration"], ""))}">'
                    f'{esc(f["migration"])}</span>')
    for flag in f.get("flags") or []:
        bits.append(f'<span class="tag tag-scope">{esc(flag)}</span>')
    return "".join(bits)


def result_line(f):
    r = f.get("result") or {}
    return " &middot; ".join(f"<strong>{esc(k.replace('_', ' '))}</strong> {esc(v)}"
                             for k, v in r.items())


def render_findings(findings):
    parts = []
    for key, title, blurb in GROUPS:
        members = [f for f in findings if group_of(f) == key]
        if not members:
            continue
        parts.append('<section>')
        parts.append(f"  <h2>{esc(title)}</h2>")
        parts.append('  <hr class="sec-rule">')
        parts.append(f'  <div class="prose"><p>{blurb}</p></div>')
        for f in members:
            ident = f.get("id") or f"#{f['seq']:02d}"
            parts.append('  <div class="finding">')
            parts.append('    <div class="finding-head">')
            parts.append(f'      <span class="fid">{esc(ident)}</span>')
            parts.append(f"      <h3>{esc(f['title'])}</h3>")
            parts.append(f'      <div class="tags">{tier_tags(f)}</div>')
            parts.append("    </div>")
            src = esc(f.get("site") or "")
            if f.get("commit"):
                src = f"<code>{esc(f['commit'])}</code> &middot; {src}"
            parts.append(f'    <p class="src">{src}</p>')
            parts.append('    <div class="prose">')
            parts.append(f"      <p>{esc(f['summary'])}</p>")
            if result_line(f):
                parts.append(f"      <p>{result_line(f)}</p>")
            if f.get("controls"):
                parts.append(f"      <p><em>Controls.</em> {esc(f['controls'])}</p>")
            if f.get("caveat"):
                parts.append('      <p class="supersede"><strong>Caveat.</strong> '
                             f"{esc(f['caveat'])}</p>")
            if f.get("reason"):
                parts.append(f"      <p><strong>Why not.</strong> {esc(f['reason'])}</p>")
            if f.get("tests"):
                parts.append(f"      <p><em>Tests.</em> {esc(f['tests'])}</p>")
            if f.get("note"):
                parts.append(f"      <p>{esc(f['note'])}</p>")
            parts.append("    </div>")
            parts.append("  </div>")
        parts.append("</section>")
    return "\n".join(parts)


def render_ledger(findings):
    rows = []
    for f in sorted(findings, key=lambda x: x["seq"]):
        commit = f"<code>{esc(f['commit'])}</code>" if f.get("commit") else "&mdash;"
        primary = next(iter((f.get("result") or {}).values()), "&mdash;")
        cls = ' class="emph"' if f["status"] == "accepted" and "-6" in str(primary) else ""
        rows.append(
            f"        <tr{cls}><td class=\"id\">{f['seq']:02d}</td><td>{commit}</td>"
            f"<td>{esc(f['title'])}</td>"
            f'<td class="num">{tier_tags(f)}</td>'
            f'<td>{esc(f["status"])}</td>'
            f'<td class="num">{esc(primary)}</td></tr>')
    return (
        '  <div class="tablewrap">\n    <table>\n'
        "      <caption>Every experiment, in order. Individual gains overlap and do not "
        "sum &mdash; several attack natural-key cost by different means. The effect column "
        "quotes each experiment's own primary measurement, which is a deterministic counter "
        "wherever wall clock was unusable. Generated from <code>perf/findings/</code>.</caption>\n"
        "      <thead><tr><th>#</th><th>Commit</th><th>Change</th><th class=\"num\">Tier</th>"
        "<th>Status</th><th class=\"num\">Primary measured effect</th></tr></thead>\n"
        "      <tbody>\n" + "\n".join(rows) + "\n      </tbody>\n    </table>\n  </div>")


def render_factbar(findings):
    accepted = sum(1 for f in findings if f["status"] == "accepted")
    facts = [("Branch", "next &middot; 3.3.0a0"), ("Dataset", "24,091 objects"),
             ("Read scenarios", "38"), ("Accepted fixes", str(accepted))]

    base = PERF / "baselines" / "large-tier1-baseline.json"
    cur = sorted((PERF / "results").glob("tier1-large-f*.json"))
    if base.exists() and cur:
        b = json.loads(base.read_text())["endpoints"]
        c = json.loads(cur[-1].read_text())["endpoints"]
        bt = sum(e["query_count"] for e in b)
        ct = sum(e["query_count"] for e in c)
        facts.append(("Queries", f"{bt:,} &rarr; {ct:,}"))

    body = "\n".join(f'    <div class="fact"><dt>{k}</dt><dd>{v}</dd></div>'
                     for k, v in facts)
    return f'  <dl class="factbar">\n{body}\n  </dl>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="render and compare without writing; exit 1 on drift")
    args = ap.parse_args()

    findings = load_findings()
    if not findings:
        print("no findings in perf/findings/", file=sys.stderr)
        return 2

    template = (PERF / "report.template.html").read_text()
    out = (template
           .replace("<!--GEN:factbar-->", render_factbar(findings))
           .replace("<!--GEN:findings-->", render_findings(findings))
           .replace("<!--GEN:ledger-->", render_ledger(findings)))

    for marker in ("<!--GEN:factbar-->", "<!--GEN:findings-->", "<!--GEN:ledger-->"):
        if marker in out:
            print(f"marker not substituted: {marker}", file=sys.stderr)
            return 2

    target = PERF / "report.html"
    if args.check:
        current = target.read_text() if target.exists() else ""
        if current != out:
            print("report.html is stale -- run perf/build_report.py", file=sys.stderr)
            return 1
        print("report.html is current")
        return 0

    target.write_text(out)
    counts = {}
    for f in findings:
        counts[group_of(f)] = counts.get(group_of(f), 0) + 1
    print(f"wrote {target.relative_to(ROOT)} from {len(findings)} findings "
          f"({', '.join(f'{k} {v}' for k, v in counts.items())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
