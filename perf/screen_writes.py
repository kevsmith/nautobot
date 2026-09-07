#!/usr/bin/env python
"""Screening pass over every REST write endpoint, normalized to cost per object.

The read screen (``screen_reads.py``) answers "which read endpoints do work per
row". This answers the same question for creates and updates, over the same
enumerated-from-the-resolver surface, so the two rank on comparable numbers.

Three things carry it, and the third is the one that makes it trustworthy.

**Enumerate from the URL resolver at run time**, as the read screen does, and
take writability from the router's own action map rather than guessing: a route
whose DRF ``actions`` has no ``post`` is not a create endpoint, and the resolver
is authoritative about that.

**Normalize to cost per created object.** A single POST is mostly fixed
overhead. Nautobot's API accepts a JSON list on a list endpoint and creates the
lot, so measuring x1 and xN gives both the fixed cost and the marginal per-object
cost -- ``(q_xN - q_x1) / (N - 1)`` -- and it is the marginal figure that reveals
per-row work. This is the write-side equivalent of the ``9 + 14n`` fit that
identified ``dcim.interfaceconnections`` on the read side.

**Coverage accounting, reported per model and never inferred.** A write screen
can under-report in a way a read screen cannot: a model whose generated payload
is rejected drops out and reads as *not a problem* rather than *not measured*.
So every model lands in exactly one bucket -- writable / payload built / payload
accepted / measured -- with the reason it stopped where it did, and the summary
prints all four counts. Quoting the endpoint total alone is the failure that put
"330 endpoints, roughly 5%" in ``perf/README.md`` for weeks.

**Isolation.** Every measured request runs inside ``transaction.atomic()`` that
is rolled back, so each model starts from byte-identical state and nothing this
instrument does survives it. That is affordable *because* the requests go
through the Django test client and therefore stay in-process -- finding 29's
"REST writes cross the process boundary" applies to a real HTTP client against
uwsgi, not to this one. Two things follow. Change logging is not hidden by the
rollback: Nautobot records ObjectChanges from synchronous ``post_save``
receivers and ``ObjectChangeMiddleware`` flushes them inside the same request,
with no ``transaction.on_commit`` anywhere on that path. But ``on_commit``
callbacks elsewhere -- five of them, including custom-field job enqueueing at
``extras/customfields.py:744`` -- never fire, so their cost is outside every
number here. ``--isolation reset`` swaps rollback for a real committed write
followed by ``perf/reset_db.sh``, which is what finding 33 made affordable; it
is the control, not the default, and it discards exactly one request after each
reset per that finding's warmup convention.

    perf/dc.sh exec -T nautobot python /source/perf/screen_writes.py \\
        --out /source/perf/results/screen-writes.json
"""

import argparse
from collections import Counter
import hashlib
import json
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.client import RequestFactory  # noqa: E402
from django.urls import get_resolver, NoReverseMatch, reverse, URLPattern, URLResolver  # noqa: E402
from rest_framework.request import Request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payloads import MODEL_SEEDS, PayloadBuilder, Unbuildable  # noqa: E402
from tier1_queries import get_perf_client, normalize_sql  # noqa: E402
from tier1w_writes import QueryCollector, Rollback  # noqa: E402

PERF_DIR = os.path.dirname(os.path.abspath(__file__))


# --- surface enumeration ----------------------------------------------------


def api_write_routes():
    """(view_name, app, viewset_cls) for every REST list route that accepts POST.

    Walks the resolver rather than any list in a file, so the matrix cannot rot
    as models come and go. ``callback.actions`` is DRF's own method-to-handler
    map, set by the router when it registered the route -- so "does this endpoint
    create" is read from the router rather than inferred from the class.
    """

    def walk(resolver, ns=()):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                child = (*ns, pattern.namespace) if pattern.namespace else ns
                yield from walk(pattern, child)
            elif isinstance(pattern, URLPattern) and pattern.name:
                yield ":".join((*ns, pattern.name)), pattern

    seen = {}
    for name, pattern in walk(get_resolver()):
        if ":" not in name or not name.endswith("-list"):
            continue
        namespace, _, _ = name.rpartition(":")
        if not namespace.endswith("-api"):
            continue
        seen.setdefault(name, (namespace[: -len("-api")], pattern))

    routes = []
    for name in sorted(seen):
        app, pattern = seen[name]
        callback = pattern.callback
        actions = getattr(callback, "actions", None) or {}
        routes.append(
            {
                "view_name": name,
                "app": app,
                "viewset": getattr(callback, "cls", None),
                "writable": "post" in actions,
            }
        )
    return routes


def serializer_and_model(viewset):
    """(serializer_class, model) for a viewset, or (None, None) with a reason."""
    if viewset is None:
        return None, None, "no viewset class on the URL callback"
    serializer_class = getattr(viewset, "serializer_class", None)
    queryset = getattr(viewset, "queryset", None)
    model = getattr(queryset, "model", None)
    if serializer_class is None:
        return None, None, "viewset declares no serializer_class"
    if model is None:
        return None, None, "viewset declares no queryset"
    return serializer_class, model, None


# --- measurement ------------------------------------------------------------


def measure_request(client, method, url, data, isolate=True):
    """Issue one write and return its profile.

    ``execute_wrapper`` rather than ``CaptureQueriesContext``: the latter reads
    ``connection.queries``, a deque capped at 9000 entries, and a bulk create can
    overflow it -- after which the slice arithmetic reports zero rather than
    failing. This is the same collector Tier 1W uses, for the same reason.
    """
    collector = QueryCollector()
    body = json.dumps(data)
    status = None
    response_body = b""
    error = None

    with connection.execute_wrapper(collector):
        start = time.perf_counter()
        try:
            if isolate:
                with transaction.atomic():
                    response = client.generic(method, url, body, content_type="application/json")
                    status = response.status_code
                    response_body = response.content
                    raise Rollback
            else:
                response = client.generic(method, url, body, content_type="application/json")
                status = response.status_code
                response_body = response.content
        except Rollback:
            pass
        except Exception as exc:  # a view that blows up is itself a finding
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - start) * 1000.0

    shapes = Counter(normalize_sql(sql) for sql in collector.sql)
    worst_shape, worst_count = (shapes.most_common(1) or [("", 0)])[0]
    return {
        "status": status,
        "error": error,
        "wall_ms": round(elapsed_ms, 2),
        "query_count": len(collector.sql),
        "db_ms": round(collector.total_seconds * 1000.0, 2),
        "duplicate_queries": sum(c - 1 for c in shapes.values() if c > 1),
        "distinct_shapes": len(shapes),
        "worst_repeat_count": worst_count,
        "worst_repeat_sql": worst_shape[:400],
        "response_bytes": len(response_body),
        "_body": response_body,
    }


def failure_detail(record):
    """A short, quotable reason a write was rejected.

    DRF reports per-field errors, and the *field* is the actionable part: it says
    whether the payload builder guessed wrong or the model wants something no
    generated payload can supply.
    """
    if record.get("error"):
        return record["error"][:300]
    body = record.get("_body") or b""
    try:
        parsed = json.loads(body.decode())
    except Exception:
        return f"HTTP {record['status']}: {body[:200].decode(errors='replace')}"
    if isinstance(parsed, dict):
        parts = []
        for key, value in list(parsed.items())[:4]:
            text = value if isinstance(value, str) else json.dumps(value)
            parts.append(f"{key}: {text[:120]}")
        return f"HTTP {record['status']}: " + "; ".join(parts)
    return f"HTTP {record['status']}: {json.dumps(parsed)[:200]}"


def created_count(record):
    """How many objects a 2xx create response reports having made."""
    try:
        parsed = json.loads((record.get("_body") or b"").decode())
    except Exception:
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict) and "id" in parsed:
        return 1
    return None


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def repeated(client, method, url, data, reps, isolate, reset):
    """Run one operation ``reps`` times and summarize.

    Query count is reported as a range when it moves, because an unstable count
    is itself the finding -- it means the operation's work depends on state the
    measurement did not control. Wall clock is reported as a median: Tier 1W's
    habit of reporting the last rep is what let a recurring GC pause land on one
    arm and not the other (finding 32).
    """
    # One discarded run before the first measured one. Not a wall-clock nicety:
    # 47 of 252 measurements in the first full run had *unstable query counts*,
    # nearly all of them updates, because process-level caches -- the natural-key
    # field lookups and the tag cache -- survive a rolled-back transaction and are
    # cold only on the first pass. The rollback restores the database, not the
    # process, so the warmup is what makes the counts deterministic, which is the
    # property the whole harness rests on.
    if not reset:
        measure_request(client, method, url, data, isolate=isolate)

    runs = []
    for _ in range(reps):
        if reset:
            reset_database(client)
            # Finding 33: the first request after a clone runs a few hundred ms
            # slow and varies by hundreds of ms between rounds. Exactly one
            # throwaway removes the whole effect -- and it doubles as the cache
            # warmup above.
            #
            # The throwaway is rolled back even here. It has to be the same write
            # to warm the right pages and the right caches, and it must leave
            # nothing behind: a committed throwaway makes the measured request
            # collide with it on every unique name, which reads as a model whose
            # payload was rejected. Rolling it back costs nothing that matters --
            # commit against rollback is -0.9%, inside variance (finding 29).
            measure_request(client, method, url, data, isolate=True)
        runs.append(measure_request(client, method, url, data, isolate=isolate))

    counts = {r["query_count"] for r in runs}
    summary = dict(runs[-1])
    summary.pop("_body", None)
    summary["wall_ms"] = round(median([r["wall_ms"] for r in runs]), 2)
    summary["db_ms"] = round(median([r["db_ms"] for r in runs]), 2)
    summary["reps"] = reps
    summary["query_count_stable"] = len(counts) == 1
    if len(counts) > 1:
        summary["query_count_range"] = [min(counts), max(counts)]
    return summary, runs[-1]


# --- reset-based isolation --------------------------------------------------
#
# `--isolation reset` cannot shell out to perf/reset_db.sh, because this script
# runs *inside* the nautobot container and that script drives `docker compose`
# from the host. So the clone is issued here, over a second connection to the
# `postgres` database -- and it has to carry the same staleness guard, because
# the hazard finding 33 identified does not go away by being harder to reach:
# the template is a database at a fixed schema, and a clone that skipped the
# check would silently hand back a schema behind the code.
#
# This is a deliberate second implementation of reset_db.sh's fingerprint. Both
# hash the same thing the same way -- the sorted list of migration file paths,
# then their contents -- and `--verify-reset` compares this one's answer against
# the value the template carries, so a drift between the two fails loudly the
# first time reset isolation is used rather than producing a quiet wrong number.

TEMPLATE_DB = os.environ.get("PERF_TEMPLATE_DB", "nautobot_pristine")


def migration_fingerprint():
    """sha256 over the tree's migration files: the path list, then the contents.

    Byte-for-byte the same input as reset_db.sh's `migration_fingerprint`, which
    does `printf '%s\n' "$list"; printf '%s\n' "$list" | xargs cat`. Names alone
    would miss a migration edited in place, which is the case most likely to
    leave the template silently wrong.
    """
    root = os.path.dirname(PERF_DIR)
    paths = []
    for dirpath, _, filenames in os.walk(os.path.join(root, "nautobot")):
        if os.path.basename(dirpath) != "migrations":
            continue
        for filename in filenames:
            if filename.endswith(".py") and filename != "__init__.py":
                paths.append(os.path.relpath(os.path.join(dirpath, filename), root))
    paths.sort()

    digest = hashlib.sha256()
    digest.update(("\n".join(paths) + "\n").encode())
    for path in paths:
        with open(os.path.join(root, path), "rb") as fh:
            digest.update(fh.read())
    return digest.hexdigest()


def _postgres_connection():
    from django.conf import settings
    import psycopg2  # deferred: only the reset path needs it

    db = settings.DATABASES["default"]
    connection_ = psycopg2.connect(
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"] or 5432,
    )
    # CREATE/DROP DATABASE cannot run inside a transaction block -- and note that
    # `with psycopg2_connection:` opens one regardless of this flag, so callers
    # use try/finally rather than the context manager.
    connection_.autocommit = True
    return connection_


def check_template(fatal=True):
    """Confirm the template exists and matches the tree's migrations."""
    expected = f"perf-migrations:{migration_fingerprint()}"
    conn = _postgres_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                (TEMPLATE_DB,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    stored = (row[0] or "").strip() if row else ""
    if stored == expected:
        return True, stored
    detail = (
        f"no template {TEMPLATE_DB!r} (or it carries no fingerprint)"
        if not stored
        else f"STALE TEMPLATE\n  template: {stored}\n  tree:     {expected}"
    )
    if fatal:
        # Refuse rather than fall back to the 49-second path, per finding 33: a
        # reset that is 1s most of the time and 49s occasionally puts a
        # 48-second spike inside a measurement loop at a moment nobody chose.
        raise SystemExit(f"{detail}\nrun: perf/restore_snapshot.sh && perf/reset_db.sh --build")
    return False, stored


def reset_database(client=None):
    """Clone the pristine template over the live database. ~1.3s (finding 33).

    The client has to be logged in again afterwards. `force_login` writes a row
    to `django_session`, and the clone replaces the database that row lives in --
    so without this every request after the first reset is anonymous, and the
    screen would measure 403s while reporting them as writes.
    """
    from django.conf import settings
    from django.db import connections

    live = settings.DATABASES["default"]["NAME"]
    # Django's own pooled connection would block the DROP; close it first and let
    # it reconnect on the next query, which is what finding 33 verified it does.
    connections.close_all()
    conn = _postgres_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{live}" WITH (FORCE)')
            cur.execute(
                f'CREATE DATABASE "{live}" OWNER {settings.DATABASES["default"]["USER"]} TEMPLATE "{TEMPLATE_DB}"'
            )
    finally:
        conn.close()
    if client is not None:
        relogin(client)


def relogin(client):
    """Re-establish the superuser session against the freshly cloned database."""
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username="perfbot",
        defaults={"is_superuser": True, "is_staff": True, "is_active": True},
    )
    if created or not user.is_superuser:
        user.is_superuser = user.is_staff = user.is_active = True
        user.save()
    client.force_login(user)


# --- the screen -------------------------------------------------------------


def screen_model(client, builder, route, context, args):
    """Measure one model's write endpoints.

    Returns (coverage_record, measurement_records). The coverage record is
    written whatever happens, so a model that could not be measured is visible
    as a model that could not be measured.
    """
    view_name, app = route["view_name"], route["app"]
    coverage = {"id": view_name, "app": app, "model": None, "stage": "writable", "reason": None}
    measurements = []

    serializer_class, model, why = serializer_and_model(route["viewset"])
    if why:
        coverage["stage"] = "no-serializer"
        coverage["reason"] = why
        return coverage, measurements
    coverage["model"] = model._meta.label_lower
    coverage["seeded"] = model._meta.label_lower in MODEL_SEEDS

    try:
        list_url = reverse(view_name)
    except NoReverseMatch as exc:
        coverage["stage"] = "no-serializer"
        coverage["reason"] = f"did not reverse: {exc}"
        return coverage, measurements

    tag = model._meta.model_name[:12]
    try:
        one, diagnostics = builder.build_create(serializer_class, model, context, count=1, tag=tag)
    except Unbuildable as exc:
        coverage["stage"] = "payload-unbuildable"
        coverage["reason"] = str(exc)
        return coverage, measurements
    except Exception as exc:  # introspecting a serializer can fail; that is data
        coverage["stage"] = "payload-unbuildable"
        coverage["reason"] = f"{type(exc).__name__}: {exc}"
        return coverage, measurements

    # Built separately, because a model can be measurable one at a time and not
    # in bulk -- a uniqueness constraint over two foreign keys needs `bulk`
    # distinct rows to point at, and a small table may not have them. Losing the
    # x1 figure over that would be the screen under-reporting itself.
    many, bulk_reason = None, None
    try:
        many, _ = builder.build_create(serializer_class, model, context, count=args.bulk, tag=tag)
    except Unbuildable as exc:
        bulk_reason = str(exc)
    except Exception as exc:
        bulk_reason = f"{type(exc).__name__}: {exc}"
    if bulk_reason:
        coverage["bulk_reason"] = bulk_reason

    coverage["stage"] = "payload-built"
    coverage["fields_populated"] = diagnostics["fields_populated"]

    # Probe before measuring: an accepted payload is the precondition for every
    # number below, and a rejected one has to be reported with its reason rather
    # than averaged into a run.
    # Always rolled back, in every isolation mode: this is a validity check, not
    # a measurement, and a probe that committed would leave a row behind in the
    # one mode whose whole point is a controlled starting state.
    probe = measure_request(client, "POST", list_url, one[0], isolate=True)
    if probe["status"] is None or probe["status"] >= 300:
        coverage["stage"] = "payload-rejected"
        coverage["reason"] = failure_detail(probe)
        coverage["payload"] = one[0]
        return coverage, measurements

    coverage["stage"] = "measured"
    isolate = args.isolation == "rollback"
    reset = args.isolation == "reset"

    # Chosen now, before any create commits. Under --isolation reset the creates
    # are real, and `first()` by pk over UUIDs would happily return one of them --
    # which the next reset then deletes, and the PATCH 404s.
    update_target = model._default_manager.order_by("pk").first()

    x1, _ = repeated(client, "POST", list_url, one[0], args.reps, isolate, reset)
    x1.update(id=view_name, app=app, model=coverage["model"], kind="create.x1", objects=1, url=list_url)
    measurements.append(x1)

    if many is not None:
        xn, last = repeated(client, "POST", list_url, many, args.reps, isolate, reset)
        made = created_count(last)
        xn.update(
            id=view_name,
            app=app,
            model=coverage["model"],
            kind=f"create.x{args.bulk}",
            objects=made,
            url=list_url,
            bulk_accepted=last["status"] is not None and last["status"] < 300,
        )
        if not xn["bulk_accepted"]:
            xn["reason"] = failure_detail(last)
            coverage["bulk_reason"] = xn["reason"]
        measurements.append(xn)

    # update: same field on every model that has one, so update costs compare.
    try:
        patch_payload, patch_field = builder.build_update(serializer_class, model, context)
    except Unbuildable as exc:
        coverage["update_reason"] = exc.reason
        if reset:
            reset_database(client)
        return coverage, measurements

    instance = update_target
    if instance is None:
        coverage["update_reason"] = "no existing row to update"
        if reset:
            reset_database(client)
        return coverage, measurements
    detail_name = view_name[: -len("-list")] + "-detail"
    try:
        detail_url = reverse(detail_name, args=[instance.pk])
    except NoReverseMatch as exc:
        coverage["update_reason"] = f"detail route did not reverse: {exc}"
        if reset:
            reset_database(client)
        return coverage, measurements

    patch, last_patch = repeated(client, "PATCH", detail_url, patch_payload, args.reps, isolate, reset)
    patch.update(
        id=view_name,
        app=app,
        model=coverage["model"],
        kind="update.x1",
        objects=1,
        url=detail_url,
        field=patch_field,
    )
    if last_patch["status"] is None or last_patch["status"] >= 300:
        patch["reason"] = failure_detail(last_patch)
        coverage["update_reason"] = patch["reason"]
    measurements.append(patch)
    if reset:
        # The last rep committed. Leave the database as this model found it.
        reset_database(client)
    return coverage, measurements


STAGES = ["writable", "no-serializer", "payload-unbuildable", "payload-built", "payload-rejected", "measured"]


def coverage_summary(routes, coverage_records):
    """Attempted / buildable / accepted / measured, each one counted not inferred."""
    by_stage = Counter(record["stage"] for record in coverage_records)
    seeded = [r["id"] for r in coverage_records if r.get("seeded") and r["stage"] == "measured"]
    reasons = Counter()
    for record in coverage_records:
        if record["stage"] != "measured" and record.get("reason"):
            reasons[record["reason"].split(":")[0][:80]] += 1
    return {
        "list_endpoints": len(routes),
        "writable": sum(1 for r in routes if r["writable"]),
        "read_only": sum(1 for r in routes if not r["writable"]),
        "attempted": len(coverage_records),
        "payload_built": by_stage["payload-built"] + by_stage["payload-rejected"] + by_stage["measured"],
        "payload_accepted": by_stage["measured"],
        "measured": by_stage["measured"],
        "by_stage": {stage: by_stage[stage] for stage in STAGES},
        # How much of the measured set only got there via the hand-maintained
        # exception list in payloads.py. Reported because a coverage number that
        # hides its exceptions is the failure this instrument exists to avoid.
        "measured_with_seed": len(seeded),
        "measured_without_seed": by_stage["measured"] - len(seeded),
        "seeded_models": sorted(seeded),
        "top_failure_reasons": reasons.most_common(15),
    }


def marginal_costs(measurements, bulk):
    """Marginal cost per created object, per model, from each model's x1/xN pair.

    ``(x_xN - x_x1) / (made - 1)`` for both query count and db time, using the
    number of objects the bulk response actually reported rather than ``bulk``:
    a partially-accepted bulk create would otherwise divide by objects that were
    never made.
    """
    pairs = {}
    for record in measurements:
        if record["kind"] not in ("create.x1", f"create.x{bulk}"):
            continue
        if record.get("status") is None or record["status"] >= 300:
            continue
        pairs.setdefault(record["model"], {})[record["kind"]] = record

    rows = []
    for model, pair in pairs.items():
        one, many = pair.get("create.x1"), pair.get(f"create.x{bulk}")
        made = (many or {}).get("objects") or 0
        if one is None or many is None or made < 2:
            continue
        rows.append(
            {
                "model": model,
                "queries_per_object": (many["query_count"] - one["query_count"]) / (made - 1),
                "db_ms_per_object": (many["db_ms"] - one["db_ms"]) / (made - 1),
                "queries_x1": one["query_count"],
            }
        )
    return rows


def print_rankings(measurements, bulk, limit=15):
    """Rank the measured models twice: by queries per object, and by db time.

    Queries per object is the screen's headline figure and it is blind to a
    model whose per-row work is a few expensive statements rather than many
    cheap ones. Finding 39 is where that mattered: on the write path query count
    and db time come apart, and an instrument that gates on one of them
    mis-rates work that moves the other. Both columns are shown in both tables
    so a model that ranks high on one and low on the other is visible as such.
    """
    rows = marginal_costs(measurements, bulk)
    if not rows:
        return
    for label, key in (
        ("queries per created object", "queries_per_object"),
        ("db ms per created object", "db_ms_per_object"),
    ):
        rows.sort(key=lambda r: (-r[key], r["model"]))
        print(f"\ntop {min(limit, len(rows))} of {len(rows)} models by {label}", file=sys.stderr)
        print(f"   {'model':44s} {'q/obj':>8} {'db ms/obj':>10} {'q x1':>6}", file=sys.stderr)
        for r in rows[:limit]:
            print(
                f"   {r['model']:44s} {r['queries_per_object']:>8.1f} "
                f"{r['db_ms_per_object']:>10.2f} {r['queries_x1']:>6}",
                file=sys.stderr,
            )


def provenance():
    try:
        with open(os.path.join(PERF_DIR, ".provenance.json")) as fh:
            return json.load(fh)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--bulk", type=int, default=10, help="objects in the bulk POST; the per-object figure comes from it"
    )
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument(
        "--isolation",
        choices=["rollback", "reset", "none"],
        default="rollback",
        help="rollback: in-process transaction, undone (default). reset: commit, then perf/reset_db.sh "
        "plus one discarded request per finding 33. none: commit and leave it -- diagnostic only.",
    )
    ap.add_argument("--include-optional", action="store_true", help="also populate optional scalar fields")
    ap.add_argument("--only", help="substring filter on view name")
    ap.add_argument("--max-endpoints", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="build payloads and report coverage; issue no requests")
    ap.add_argument(
        "--verify-reset",
        action="store_true",
        help="check the clone template against the tree's migrations and exit; use after changing either fingerprint",
    )
    args = ap.parse_args()

    if args.verify_reset:
        ok, stored = check_template(fatal=False)
        print(f"template {TEMPLATE_DB}: {stored or '<none>'}", file=sys.stderr)
        print(f"tree:      perf-migrations:{migration_fingerprint()}", file=sys.stderr)
        raise SystemExit(0 if ok else 1)

    if args.isolation == "reset":
        check_template()

    routes = api_write_routes()
    writable = [r for r in routes if r["writable"]]
    targets = writable
    if args.only:
        targets = [r for r in targets if args.only in r["view_name"]]
    if args.max_endpoints:
        targets = targets[: args.max_endpoints]

    print(
        f"{len(routes)} list endpoints, {len(writable)} accept POST; screening {len(targets)} "
        f"(bulk={args.bulk}, reps={args.reps}, isolation={args.isolation}"
        f"{', dry-run' if args.dry_run else ''})",
        file=sys.stderr,
    )

    client = get_perf_client()
    if args.isolation == "reset":
        # Start from the template, so the first model's PATCH target is a row the
        # resets that follow will not remove.
        reset_database(client)
    User = get_user_model()
    user = User.objects.get(username="perfbot")
    request = Request(RequestFactory().post("/api/"))
    request.user = user
    context = {"request": request, "depth": 0}
    builder = PayloadBuilder(include_optional=args.include_optional)

    coverage_records = []
    measurements = []
    started = time.perf_counter()

    for i, route in enumerate(targets, 1):
        if args.dry_run:
            coverage, got = dry_run_model(builder, route, context, args)
        else:
            coverage, got = screen_model(client, builder, route, context, args)
        coverage_records.append(coverage)
        measurements.extend(got)

        create = next((m for m in got if m["kind"] == "create.x1"), {})
        note = coverage["stage"] if coverage["stage"] != "measured" else f"q={create.get('query_count')}"
        print(f"[{i}/{len(targets)}] {route['view_name']:52s} {note}", file=sys.stderr)

        # Written every endpoint: a run that dies partway should still leave
        # usable data, and the coverage numbers are only true of what ran.
        write_out(args, routes, coverage_records, measurements, started)

    write_out(args, routes, coverage_records, measurements, started)
    summary = coverage_summary(routes, coverage_records)
    print(
        f"\ncoverage: {summary['writable']} writable / {summary['attempted']} attempted / "
        f"{summary['payload_built']} payload built / {summary['payload_accepted']} payload accepted / "
        f"{summary['measured']} measured "
        f"({summary['measured_without_seed']} from field metadata alone, "
        f"{summary['measured_with_seed']} needing a seed entry)",
        file=sys.stderr,
    )
    for reason, count in summary["top_failure_reasons"]:
        print(f"   {count:>4}  {reason}", file=sys.stderr)
    print_rankings(measurements, args.bulk)
    print(
        f"wrote {args.out} -- {len(measurements)} measurements in {time.perf_counter() - started:.0f}s",
        file=sys.stderr,
    )


def dry_run_model(builder, route, context, args):
    """Build the payload and stop. No requests, so this reports buildability only.

    Buildability is not validity -- a payload the builder produced can still be
    rejected -- so a dry run's coverage stops one bucket short of the real one,
    and says so by never reporting the 'measured' stage.
    """
    coverage = {"id": route["view_name"], "app": route["app"], "model": None, "stage": "writable", "reason": None}
    serializer_class, model, why = serializer_and_model(route["viewset"])
    if why:
        coverage.update(stage="no-serializer", reason=why)
        return coverage, []
    coverage["model"] = model._meta.label_lower
    coverage["seeded"] = model._meta.label_lower in MODEL_SEEDS
    try:
        payloads, diagnostics = builder.build_create(
            serializer_class, model, context, count=1, tag=model._meta.model_name[:12]
        )
    except Unbuildable as exc:
        coverage.update(stage="payload-unbuildable", reason=str(exc))
        return coverage, []
    except Exception as exc:
        coverage.update(stage="payload-unbuildable", reason=f"{type(exc).__name__}: {exc}")
        return coverage, []
    coverage.update(stage="payload-built", fields_populated=diagnostics["fields_populated"], payload=payloads[0])
    return coverage, []


def write_out(args, routes, coverage_records, measurements, started):
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(
            {
                "schema": 1,
                "provenance": provenance(),
                "bulk": args.bulk,
                "reps": args.reps,
                "isolation": args.isolation,
                "include_optional": args.include_optional,
                "dry_run": args.dry_run,
                "elapsed_s": round(time.perf_counter() - started, 1),
                "coverage": coverage_summary(routes, coverage_records),
                "models": coverage_records,
                "measurements": measurements,
            },
            fh,
            indent=2,
            sort_keys=True,
            default=str,
        )


if __name__ == "__main__":
    main()
