#!/usr/bin/env python3
"""Verify that perf/report.md says nothing the committed data does not support.

`build_report.py --check` proves the report was generated from the current
sources. It does not prove those sources agree with each other, or that a figure
in the report can be traced back to a file. This does.

Every check here exists because something was wrong once:

    A reverted commit was published as an accepted change. Finding 22 recorded
    `9b8831711`, whose own subject says "BREAKS 13 TESTS", and it was reverted
    by `9fbb283b9`; what landed was `3c683a405`. Nothing noticed until the
    commit column was pointed at a branch and the SHA had to resolve on it.

    A query counter silently returned zero. `CaptureQueriesContext` slices a
    deque with maxlen=9000 shared per process, so a long run reported
    status-200 responses with full bodies as issuing no queries -- and it
    degraded before it failed, reporting 495 of one endpoint's 1,067 queries
    while looking ordinary. See finding 45.

    An aggregate was quoted for weeks against a tree three findings behind the
    branch, because staleness lived in someone's memory rather than in a file.

    python3 perf/scripts/verify_report.py          # exit 1 on any problem
"""

import glob
import json
import pathlib
import re
import shutil
import subprocess
import sys

import yaml

PERF = pathlib.Path(__file__).resolve().parent.parent
ROOT = PERF.parent
# Resolved once, so every call below passes a full path. The only variable parts
# of these invocations are commit SHAs and refs read from committed YAML, which
# git treats as revisions -- there is no shell and nothing to inject into.
GIT = shutil.which("git") or "/usr/bin/git"


def git(*args, ok_only=False):
    """Run git and return stdout, or whether it succeeded."""
    proc = subprocess.run([GIT, *args], cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    return proc.returncode == 0 if ok_only else proc.stdout


def check_commits(report, problems):
    """Every SHA in the report must resolve on the branch the report names."""
    branch = re.search(r"Every commit above is on `([^`]+)`", report)
    if not branch:
        problems.append("the accepted table does not name the branch its commits are on")
        return 0
    shas = re.findall(r"\| `([0-9a-f]{7,40})` \|", report)
    if not shas:
        problems.append("no commit SHAs found in the report")
    for sha in shas:
        if not git("merge-base", "--is-ancestor", sha, branch.group(1), ok_only=True):
            problems.append(f"{sha} is not an ancestor of {branch.group(1)}")
    return len(shas)


def check_reverted(problems):
    """No finding may point at a commit that was later reverted."""
    log = git("log", "--format=%s", "next..perf/experiments")
    reverted = {line[len('Revert "') :].rstrip('"') for line in log.split("\n") if line.startswith('Revert "')}
    n = 0
    for path in sorted(glob.glob(str(PERF / "findings" / "*.yml"))):
        d = yaml.safe_load(pathlib.Path(path).read_text())
        for field in ("commit", "recommended_commit"):
            sha = d.get(field)
            if not sha:
                continue
            n += 1
            subject = git("log", "-1", "--format=%s", sha).strip()
            if not subject:
                problems.append(f"finding {d['seq']}: {field} {sha} does not resolve")
            elif subject in reverted:
                problems.append(f"finding {d['seq']}: {field} {sha} was REVERTED -- {subject[:60]}")
    return n


def check_baselines(problems):
    """No committed baseline may contain a record that cannot be a measurement."""
    n = 0
    for path in sorted(glob.glob(str(PERF / "baselines" / "*.json"))):
        try:
            data = json.loads(pathlib.Path(path).read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"{pathlib.Path(path).name}: unreadable -- {exc}")
            continue
        records = data.get("endpoints") or data.get("measurements")
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            n += 1
            if rec.get("status") == 200 and rec.get("query_count") == 0 and (rec.get("response_bytes") or 0) > 0:
                problems.append(
                    f"{pathlib.Path(path).name}: {rec.get('id')} is 200 with a body and zero "
                    "queries -- instrument failure, see finding 45"
                )
    return n


def check_cumulative(report, problems):
    """Every figure in the cumulative table must be recomputable from its source."""
    index = PERF / "baselines" / "cumulative.json"
    if not index.exists():
        problems.append("no baselines/cumulative.json")
        return 0
    spec = json.loads(index.read_text())
    n = 0
    for agg in spec.get("aggregates", []):
        if agg.get("baseline_file"):

            def totals(name):
                return {
                    e["id"]: e["query_count"] for e in json.loads((PERF / "baselines" / name).read_text())["endpoints"]
                }

            b, c = totals(agg["baseline_file"]), totals(agg["current_file"])
            shared = set(b) & set(c)
            n += 1
            if f"{sum(b[k] for k in shared):,} -> {sum(c[k] for k in shared):,}" not in report:
                problems.append(f"{agg['key']}: query total is not the one in the report")
        if agg.get("file"):
            t = json.loads((PERF / "baselines" / agg["file"]).read_text())["totals"]["queries"]
            n += 1
            if f"{t['baseline']:,.0f} -> {t['current']:,.0f}" not in report:
                problems.append(f"{agg['key']}: query total is not the one in the report")
        if not agg.get("baseline_file") and not agg.get("file"):
            # Hand-entered figures. Nothing recomputes them, so the most this can do
            # is refuse to let that pass unremarked.
            n += 1
            problems.append(
                f"{agg['key']}: figures are hand-entered in cumulative.json with no source "
                "file, so no check here can recompute them"
            )
        if agg.get("stale") and agg.get("stale") not in report:
            problems.append(f"{agg['key']} is marked stale and the report does not say so")
    return n


def check_reasons(report, problems):
    """Every Reason cell must be a finding's own words, not the report's."""
    allowed = set()
    for path in glob.glob(str(PERF / "findings" / "*.yml")):
        d = yaml.safe_load(pathlib.Path(path).read_text())
        for field in ("basis", "wall_clock"):
            if d.get(field):
                allowed.add(str(d[field]).strip())
    cells = re.findall(r"^\| \d\d \| \[.*?\]\(methodology\.md#.*?\) \| .*? \| (.*?) \|$", report, re.M)
    for cell in cells:
        text = cell.rsplit(" | ", 1)[0].strip() if " | " in cell else cell.strip()
        if text not in allowed:
            problems.append(f"Reason cell not traceable to a finding: {text[:60]!r}")
    return len(cells)


def check_stock_commit(problems):
    """The stock ref must still resolve to the commit the figures were measured against."""
    index = PERF / "baselines" / "cumulative.json"
    if not index.exists():
        return 0
    spec = json.loads(index.read_text())
    ref, recorded = spec.get("stock_ref"), spec.get("stock_commit")
    if not ref or not recorded:
        problems.append("cumulative.json does not record the stock ref and commit")
        return 0
    now = git("rev-parse", f"--short={len(recorded)}", ref).strip()
    if now != recorded:
        problems.append(
            f"{ref} now resolves to {now}, but every aggregate was measured against {recorded} -- "
            "the report describes a tree that has moved, and the figures want re-measuring"
        )
    return 1


def main():
    report = (PERF / "report.md").read_text()
    problems = []
    counts = {
        "commit SHAs resolve on the demo branch": check_commits(report, problems),
        "finding commits are not reverted": check_reverted(problems),
        "baseline records are plausible": check_baselines(problems),
        "cumulative figures recompute": check_cumulative(report, problems),
        "Reason cells trace to a finding": check_reasons(report, problems),
        "the stock ref still resolves to the measured commit": check_stock_commit(problems),
    }
    for label, n in counts.items():
        print(f"  {n:>5} {label}")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"\n{sum(counts.values())} checks, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
