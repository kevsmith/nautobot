#!/usr/bin/env python3
"""Generate perf/recommended -- the upstream-facing replay of perf/verified.

perf/verified is the measurement arm of record. It carries the application-code
half of every accepted change, in the order the changes were made, and its commit
messages point back at perf/experiments by SHA:

    Application-code portion of 1974922f0 from perf/experiments.

That is the right message for a branch whose reader has both branches checked
out, and the wrong one for a reviewer who has neither. A SHA on another branch is
not resolvable, and it is not a justification. It also carries one commit that
installs the measurement environment under development/, which belongs to the
harness rather than to any finding.

So this script replays perf/verified onto a fresh branch, keeping the trees
byte-identical under nautobot/ and replacing every message with one composed from
perf/findings/*.yml -- the defect, what the instruments read, the release-note
caveat where there is one, the risk tier in words, and a single pointer naming the
perf/experiments *branch* and the finding number rather than a SHA.

The replay happens in a separate git worktree, so the caller's working tree and
branch are never touched. Nothing is resolved automatically: a cherry-pick
conflict stops the run and names the paths.

    python3 perf/scripts/build_branch.py                      # build perf/recommended
    python3 perf/scripts/build_branch.py --force              # delete and rebuild it
    python3 perf/scripts/build_branch.py --write-findings      # + record the new SHAs

The finding files are hand-maintained, with folded scalars a yaml round-trip
would reflow, so --write-findings inserts one `recommended_commit:` line directly
after the existing `commit:` line and touches nothing else.
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import build_report as report

WIDTH = 72

# The one source commit with no finding behind it. Identified by content rather
# than by SHA: it is the commit that touches nothing under nautobot/. Recognising
# it structurally means a second harness-only commit cannot slip through, and a
# rebase of perf/verified does not invalidate this script.
#
# Its three development/ paths are disjoint from every path any other verified
# commit touches, so skipping it cannot perturb the tree the next commit applies
# to -- development/ simply stays at its base content.
ENV_ONLY_PATHS = (
    "development/docker-compose.perf.yml",
    "development/nautobot_config.py",
    "development/uwsgi-perf.ini",
)

# Subjects rewritten for the upstream reader. Two reasons, and only these two:
#
#   * a subject that references another finding by number tells a reviewer
#     nothing, because they cannot read that finding (seq 34);
#   * the title is longer than 72 columns, so it needs a shorter phrasing that
#     says the same thing rather than a truncation (seq 1, 30, 31, 36, 42).
#
# Every rewrite drops a trailing clause that only the perf/experiments history
# explains. The leading clause of each was already a serviceable subject.
SUBJECT_REWRITES = {
    1: (
        "Make the natural-key fallback lazy so it is not computed per row",
        "title is 79 columns; drops 'and discarded', which the summary states",
    ),
    30: (
        "Prefetch both ends of an interface connection",
        "title is 83 columns; the dropped clause is why, and the body says it",
    ),
    31: (
        "Fix the tag cache in serialize_object, which never fired when empty",
        "title is 82 columns; same defect, stated shorter",
    ),
    34: (
        "Prefetch the cable walk for power- and console-connections",
        "title is 93 columns and cites 'finding 30', unresolvable upstream",
    ),
    36: (
        "Prefetch the cable's terminations for cable-to-cable-terminations",
        "title is 76 columns; drops 'at depth 1', which the summary states",
    ),
    48: (
        "Defer cable path rebuilds when applying cable terminations",
        "title is 82 columns and cites 'findings 7/11, 34 and 36', unresolvable upstream",
    ),
    42: (
        "Route natural_key() through the optimized ancestor walk",
        "title is 76 columns; 'Nautobot had already optimized' reads as lab note",
    ),
    53: (
        "Select_related the device list's primary IPs",
        "title is 76 columns; drops 'which its own table cannot see', which the summary explains",
    ),
}


class BuildError(RuntimeError):
    """Anything that should stop the run with a message rather than a traceback."""


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def git(*args, cwd, check=True):
    # Fixed argv, no shell, and every argument is a ref or path this script
    # derived from the repository itself -- nothing here comes from a user.
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 -- PATH lookup is wanted; git is not at a fixed location
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr.strip()}")
    return proc


def out(*args, cwd):
    return git(*args, cwd=cwd).stdout.strip()


def lines(*args, cwd):
    return [ln for ln in out(*args, cwd=cwd).splitlines() if ln]


def rev_exists(ref, *, cwd):
    return git("rev-parse", "--verify", "--quiet", ref, cwd=cwd, check=False).returncode == 0


# --------------------------------------------------------------------------
# message composition
# --------------------------------------------------------------------------


def wrap(text, initial="", subsequent=None, width=WIDTH):
    """Wrap to `width`, keeping paragraph breaks and indented blocks verbatim.

    Finding summaries are prose except where they are not: seq 31's quotes the
    offending expression as an indented code line, and re-flowing that would
    destroy the only part of the summary that has to be read literally. Any
    paragraph whose first character is whitespace is passed through untouched.
    """
    subsequent = initial if subsequent is None else subsequent
    result = []
    for para in str(text).split("\n\n"):
        para = para.rstrip()
        if not para:
            continue
        if result:
            result.append("")
        if para[0].isspace():
            result += [ln.rstrip() for ln in para.split("\n")]
            continue
        lead = initial if not any(result) else subsequent
        result += textwrap.wrap(
            " ".join(para.split()),
            width=width,
            initial_indent=lead,
            subsequent_indent=subsequent,
            break_long_words=False,
            break_on_hyphens=False,
        )
    return result


def subject_of(f):
    """The finding's title, or the rewrite recorded for it."""
    rewrite = SUBJECT_REWRITES.get(f["seq"])
    return rewrite[0] if rewrite else f["title"]


def instrument_block(f):
    """wall clock plus the core `result` keys, as an aligned two-column list.

    CORE_RESULT_KEYS is the report's own filter between an instrument reading
    and the working that produced it. The working belongs in the finding, which
    the pointer at the foot of the message names.
    """
    rows = [("wall clock", f["wall_clock"])]
    rows += [
        (key.replace("_", " "), value)
        for key, value in (f.get("result") or {}).items()
        if key in report.CORE_RESULT_KEYS
    ]
    label = max(len(name) for name, _ in rows)
    block = []
    for name, value in rows:
        lead = f"  {name.ljust(label)}  "
        block += wrap(value, initial=lead, subsequent=" " * len(lead))
    return block


def risk_block(f):
    """The behaviour tier, migration tier and flags, spelled out.

    Derived from report.tiers() so the message cannot disagree with the report's
    own table; that returns them wrapped in markdown backticks, which is the
    only thing stripped here.
    """
    codes = [code.strip("`") for code in report.tiers(f).split()]
    spelled = [f"{code} ({report.TIER_HELP[code]})" for code in codes if code in report.TIER_HELP]
    block = wrap("Risk: " + "; ".join(spelled) + ".")
    for flag in codes:
        if flag in report.FLAG_HELP:
            block += ["", *wrap(f"Flagged {flag}: {report.FLAG_HELP[flag]}")]
    return block


def message_for(f):
    """The whole commit message: subject, blank line, wrapped body."""
    body = wrap(f["summary"])
    body += ["", *wrap(f"Accepted on: {report.basis(f)}")]
    body += ["", *instrument_block(f)]
    if f.get("caveat"):
        body += ["", *wrap(f"Release note. {f['caveat']}")]
    body += ["", *risk_block(f)]
    # Exactly one pointer, and it names the branch rather than a commit on it:
    # a SHA on another branch is the defect this branch exists to fix.
    body += [
        "",
        *wrap(
            f"The full record for this change -- how it was measured, against what "
            f"controls, and the approaches that were tried and dropped -- is "
            f"finding {f['seq']} on the perf/experiments branch."
        ),
    ]
    return "\n".join([subject_of(f), "", *body]) + "\n"


# --------------------------------------------------------------------------
# source commit -> finding
# --------------------------------------------------------------------------

PORTION_OF = re.compile(r"Application-code portion of ([0-9a-f]{7,40}) from perf/experiments")
FINDING_NO = re.compile(r"^Finding (\d+)\b", re.MULTILINE)


def resolve(source_commits, findings, *, cwd):
    """Pair each source commit with its finding, or mark it for exclusion.

    Three message shapes on perf/verified, in descending order of preference:

      1. 20 commits name their perf/experiments source SHA verbatim, and that
         SHA is the `commit` field of exactly one accepted product finding. This
         is the authoritative link -- matching on subject prose gets seq 4 and 5
         backwards, because each one's subject describes the other's title.
      2. two commits carry full messages opening "Finding NN".
      3. one commit touches nothing under nautobot/ and is the environment
         carrier, which has no finding by design.

    Anything that matches none of the three is an error, not a skip.
    """
    by_commit, by_seq = {}, {}
    for f in findings:
        by_seq[f["seq"]] = f
        if f.get("commit"):
            by_commit.setdefault(str(f["commit"]), f)

    pairs = []
    for sha in source_commits:
        message = out("log", "-1", "--format=%B", sha, cwd=cwd)
        paths = lines("show", "--name-only", "--format=", sha, cwd=cwd)
        product = [p for p in paths if p.startswith("nautobot/")]

        found = None
        match = PORTION_OF.search(message)
        if match:
            for recorded, f in by_commit.items():
                if recorded.startswith(match.group(1)) or match.group(1).startswith(recorded):
                    found = f
                    break
            if found is None:
                raise BuildError(
                    f"{sha[:9]} names perf/experiments {match.group(1)}, which is not the "
                    f"`commit` of any accepted product finding"
                )
        else:
            match = FINDING_NO.search(message)
            if match:
                found = by_seq.get(int(match.group(1)))
                if found is None:
                    raise BuildError(f"{sha[:9]} cites finding {match.group(1)}, which is not accepted product")

        if found is None:
            if product:
                raise BuildError(f"{sha[:9]} maps to no finding but touches application code: {', '.join(product)}")
            pairs.append((sha, None, sorted(paths)))
        else:
            pairs.append((sha, found, sorted(paths)))

    picked = [f["seq"] for _, f, _ in pairs if f]
    if len(picked) != len(set(picked)):
        raise BuildError(f"a finding is claimed by two source commits: {sorted(picked)}")
    missing = sorted(set(by_seq) - set(picked))
    if missing:
        raise BuildError(f"accepted product findings with no source commit: {missing}")
    return pairs


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------


def prepare_worktree(root, worktree, target, base, force):
    if rev_exists(f"refs/heads/{target}", cwd=root) and not force:
        raise BuildError(f"branch {target} already exists. Pass --force to delete and rebuild it.")
    if worktree.exists() and not force:
        raise BuildError(f"worktree path {worktree} already exists. Pass --force to replace it.")

    if force:
        registered = out("worktree", "list", "--porcelain", cwd=root)
        if f"worktree {worktree}" in registered:
            git("worktree", "remove", "--force", str(worktree), cwd=root)
        if worktree.exists():
            shutil.rmtree(worktree)
        git("worktree", "prune", cwd=root)
        if rev_exists(f"refs/heads/{target}", cwd=root):
            git("branch", "-D", target, cwd=root)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", target, str(worktree), base, cwd=root)


def strip_non_product(worktree, sha):
    """Drop everything the pick staged outside nautobot/, however it changed.

    reset returns the index to HEAD, checkout returns the worktree to the index
    (and fails harmlessly for a path HEAD never had), clean removes what is left
    untracked. Between them they undo an add, a modify or a delete.
    """
    staged = lines("diff", "--cached", "--name-only", cwd=worktree)
    foreign = sorted({p for p in staged if not p.startswith("nautobot/")})
    if not foreign:
        return []
    git("reset", "-q", "HEAD", "--", *foreign, cwd=worktree)
    git("checkout", "-q", "--", *foreign, cwd=worktree, check=False)
    git("clean", "-qfd", "--", *foreign, cwd=worktree)
    if lines("status", "--porcelain", "--", *foreign, cwd=worktree):
        raise BuildError(f"{sha[:9]}: could not restore {', '.join(foreign)}")
    return foreign


def replay(worktree, pairs):
    """Cherry-pick each mapped commit and commit it under a generated message."""
    built = []
    for sha, finding, _paths in pairs:
        if finding is None:
            print(f"  skip   {sha[:9]}  environment only, no finding")
            continue

        pick = git("cherry-pick", "-n", sha, cwd=worktree, check=False)
        if pick.returncode != 0:
            conflicts = lines("diff", "--name-only", "--diff-filter=U", cwd=worktree)
            raise BuildError(
                f"cherry-pick of {sha[:9]} (finding {finding['seq']}) conflicted.\n"
                f"  conflicting paths: {', '.join(conflicts) or '(none reported)'}\n"
                f"  worktree left in place for inspection: {worktree}\n"
                f"  git error: {pick.stderr.strip()}"
            )

        dropped = strip_non_product(worktree, sha)
        if git("diff", "--cached", "--quiet", cwd=worktree, check=False).returncode == 0:
            raise BuildError(f"{sha[:9]} (finding {finding['seq']}) staged nothing under nautobot/")

        message = worktree / ".git-commit-message"
        message.write_text(message_for(finding), encoding="utf-8")
        git("commit", "-q", "--no-verify", "--cleanup=whitespace", "-F", str(message), cwd=worktree)
        message.unlink()

        new = out("rev-parse", "HEAD", cwd=worktree)
        note = f"  dropped {', '.join(dropped)}" if dropped else ""
        print(f"  pick   {sha[:9]} -> {new[:9]}  finding {finding['seq']:02d}{note}")
        built.append((finding, new))
    return built


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def verify(root, target, source, base, expected):
    """The three checks that say the replay is faithful, plus the subject list."""
    failures = []
    print("\nVerification")

    diff = lines("diff", "--name-only", source, target, cwd=root)
    scope_ok = sorted(diff) == sorted(ENV_ONLY_PATHS)
    print(f"  [{'ok' if scope_ok else 'FAIL'}] diff {source}..{target} touches only the environment paths")
    for path in sorted(diff):
        marker = " " if path in ENV_ONLY_PATHS else "  <-- unexpected"
        print(f"           {path}{marker}")
    if not scope_ok:
        failures.append("git diff shows application code changed by the rebuild")

    ancestry = git("merge-base", "--is-ancestor", base, target, cwd=root, check=False).returncode == 0
    print(f"  [{'ok' if ancestry else 'FAIL'}] {base} is an ancestor of {target}")
    if not ancestry:
        failures.append(f"{target} is not descended from {base}")

    shas = lines("rev-list", "--reverse", f"{base}..{target}", cwd=root)
    count_ok = len(shas) == expected
    print(f"  [{'ok' if count_ok else 'FAIL'}] {len(shas)} commits on top of {base} (expected {expected})")
    if not count_ok:
        failures.append(f"expected {expected} commits, got {len(shas)}")

    merges = lines("rev-list", "--merges", f"{base}..{target}", cwd=root)
    print(f"  [{'ok' if not merges else 'FAIL'}] no merge commits")
    if merges:
        failures.append(f"{len(merges)} merge commits")

    strays = []
    for sha in shas:
        for path in lines("show", "--name-only", "--format=", sha, cwd=root):
            if not path.startswith("nautobot/"):
                strays.append(f"{sha[:9]} {path}")
    print(f"  [{'ok' if not strays else 'FAIL'}] every commit touches only nautobot/")
    for stray in strays:
        print(f"           {stray}")
    if strays:
        failures.append(f"{len(strays)} paths outside nautobot/")

    print(f"\nSubjects on {target}, oldest first")
    for n, sha in enumerate(shas, 1):
        print(f"  {n:2d}. {out('log', '-1', '--format=%s', sha, cwd=root)}")

    return failures


# --------------------------------------------------------------------------
# writing the mapping back into the findings
# --------------------------------------------------------------------------


def write_findings(built):
    """Insert `recommended_commit:` after `commit:`, one line, nothing else.

    build_report.commit_of() prefers recommended_commit over commit, so the
    report's Commit column follows the upstream-facing branch once this is
    written. Deliberately line-based: these files carry folded scalars that a
    yaml load/dump round-trip reflows, and the reflow is the whole reason the
    report is generated from them rather than the other way round.
    """
    changed = []
    for finding, sha in built:
        matches = sorted((report.PERF / "findings").glob(f"{finding['seq']:02d}-*.yml"))
        if len(matches) != 1:
            raise BuildError(f"finding {finding['seq']}: expected one yml file, found {len(matches)}")
        path = matches[0]
        text = path.read_text(encoding="utf-8")
        rows = text.split("\n")
        at = [i for i, row in enumerate(rows) if row.startswith("commit:")]
        if len(at) != 1:
            raise BuildError(f"{path.name}: expected one top-level `commit:` line, found {len(at)}")
        if any(row.startswith("recommended_commit:") for row in rows):
            rows = [row for row in rows if not row.startswith("recommended_commit:")]
            at = [i for i, row in enumerate(rows) if row.startswith("commit:")]
        rows.insert(at[0] + 1, f"recommended_commit: {sha[:9]}")
        path.write_text("\n".join(rows), encoding="utf-8")
        changed.append(path)
    print(f"\nWrote recommended_commit into {len(changed)} finding files")


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="perf/recommended", help="branch to build (default: %(default)s)")
    ap.add_argument("--source", default="perf/verified", help="branch to replay (default: %(default)s)")
    ap.add_argument("--base", default="next", help="commit to build on (default: %(default)s)")
    ap.add_argument(
        "--worktree",
        default=None,
        help="where to check the new branch out (default: ../nautobot-recommended)",
    )
    ap.add_argument("--force", action="store_true", help="delete and recreate the branch and worktree")
    ap.add_argument("--keep-worktree", action="store_true", help="leave the worktree registered on success")
    ap.add_argument("--write-findings", action="store_true", help="record the new SHAs in perf/findings/*.yml")
    args = ap.parse_args()

    root = pathlib.Path(out("rev-parse", "--show-toplevel", cwd=pathlib.Path(__file__).resolve().parent))
    worktree = pathlib.Path(args.worktree) if args.worktree else root.parent / "nautobot-recommended"
    worktree = worktree.expanduser().resolve()

    findings = [f for f in report.load_findings() if f["status"] == "accepted" and report.is_product_change(f)]
    print(f"{len(findings)} accepted product findings: {' '.join(str(f['seq']) for f in findings)}")

    source_commits = lines("rev-list", "--reverse", f"{args.base}..{args.source}", cwd=root)
    print(f"{len(source_commits)} commits on {args.source} above {args.base}\n")
    pairs = resolve(source_commits, findings, cwd=root)

    prepare_worktree(root, worktree, args.target, args.base, args.force)
    try:
        built = replay(worktree, pairs)
    except BuildError:
        raise
    failures = verify(root, args.target, args.source, args.base, len(findings))

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nWorktree left in place: {worktree}")
        return 1

    if args.write_findings:
        write_findings(built)

    if args.keep_worktree:
        print(f"\nWorktree kept: {worktree}")
    else:
        git("worktree", "remove", str(worktree), cwd=root)
        print(f"\nWorktree removed; branch {args.target} remains at {out('rev-parse', args.target, cwd=root)[:9]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        sys.exit(1)
