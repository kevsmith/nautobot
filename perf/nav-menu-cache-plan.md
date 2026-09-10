# Plan: cache the rendered nav menu per user

`inc/nav_menu.html` renders on every chrome-bearing request and costs **~20.4 ms** — measured at
20.1–20.6 ms across six unrelated views, so it is a fixed cost rather than a page-specific one.
Finding 57 established there is no hot spot inside it: 160 compiled nodes expand to **3,629 node
renders** averaging ~7 µs, which is Django interpreting the template. The only levers are fewer
nodes or fewer renders. This plan takes the second.

Two measurements set the scale:

    rendered nav_menu.html   267,664 bytes   (67% of a 401KB device-list page)
      gzipped                  7,232 bytes
    render cost                 20.4 ms
    build cost                   1.1 ms superuser / 2.45 ms normal user
    outcome fingerprint          0.29 ms superuser / ~1.5 ms normal user

## Design

    key = f"nav_menu:{user.pk}:{registry_fp}:{perm_fp}"

    registry_fp   sha256 over the name-sorted registry + sorted(settings.PLUGINS). Once per process.
    perm_fp       sha256 over the ~180 has_one_or_more_perms outcomes, walked in NAME-SORTED order.
                  Per request. Recomputed every time, so no invalidation logic exists or is needed.
    user.pk       isolation. B can never read A's entry, structurally rather than by fingerprint
                  correctness.

Cached fragment: **structure only**. Everything per-request is applied client-side from a tiny
payload:

| per-request value | source | applied by |
|---|---|---|
| active link | server, one string | client adds `nb-sidenav-link-active` |
| favourites | `user.navbar_favorites_link_list` | client adds `active` to star buttons |
| version-control block | plugin state | rendered per request, outside the fragment |

## Why the key is shaped this way

**`user.pk`, not username** — usernames are mutable, so a rename would orphan an entry rather than
invalidate it, and PKs sidestep any key-escaping question.

**Outcome fingerprint, not a hash of `ObjectPermission` rows.** The menu is a pure function of the
180 check outcomes, so identical outcomes imply an identical menu definitionally. Hashing the rows
instead over-fragments: two users whose permissions are spelled differently but grant the same
access get different keys and identical menus. Constraints are worse — they affect *which objects*
a user sees, not which menu items, so including them splits the cache for no menu difference.

**A per-user prefix does not remove the need for the fingerprints.** They do different jobs:
the prefix gives isolation, the fingerprints give staleness. Without `perm_fp`, granting a
permission would not take effect until the entry expired.

**Sort before iterating.** Dict order is insertion order and depends on app `ready()` sequence.
Disagreement between workers would cause cache misses rather than wrong menus — a performance bug,
not a correctness one — but sorting is free.

## Todo

### Phase 1 — safety first

- [ ] `perm_fingerprint(user)`: walk registry tabs/groups/items in **name-sorted** order, collect
      the 180 booleans, pack to 23 bytes, sha256.
- [ ] `registry_fingerprint()`: sha256 over the sorted registry plus `sorted(settings.PLUGINS)`,
      memoised at module level.
- [ ] Test: the same user's fingerprint **changes** when an `ObjectPermission` is granted or revoked.
      This is the mid-session requirement and the one that would silently fail.
- [ ] Test: two users differing by exactly one permission get different fingerprints. Encode
      finding 52's pair — a broad non-superuser sees 15/42/122 against a superuser's 15/42/123.
- [ ] Test: adding a tab to the registry changes `registry_fp`.

### Phase 2 — split the per-request parts out of the template

- [ ] Extract `resolve_active_link(request)` from `_build_nav_menu` lines 120–146 and 167–170.
      Prefer an exact match of the current path against a startup `frozenset` of item links — the
      registry stores **already-reversed URLs**, so no reversing is needed — else fall back to
      `related_list_view_link`.
- [ ] **Keep the `HX-Current-URL` lookup.** Measured: the four real HTMX paths (list rows, embedded
      create/update, `component_id` component loads, embedded search) all render partials with no
      chrome, so the branch is currently unreachable. But an app could register an htmx view that
      renders full chrome, and `hx-get` appears in eight core templates. Removing it converts a
      harmless defensive line into a latent bug.
- [ ] Lift the `nautobot_version_control` block (lines 32–112, **more than a third of the file**)
      into its own per-request include. It carries `active_branch` and `active_time_travel_date`
      and five `{% url %}` reversals; keying the cache on branch would explode cardinality.
- [ ] Render the cacheable fragment with `is_active` forced False and favourites empty.
- [ ] Delete the dead check at line 172: `item_details.permissions` is never present on built
      items — they carry only `{is_active, name, weight}` — so the `disabled` class can never be
      applied. It reads like a safety net that has never fired.

### Phase 3 — client-side application

- [ ] Ship the active link and favourites list in a small `json_script` payload.
- [ ] Inline script immediately after the menu markup: `querySelector('a[href="…"]')` adds the
      active class; each favourite's button gets `active`. Inline and adjacent so it runs before
      paint and there is no visible flash.
- [ ] Test: on a **detail** page the correct list item is highlighted — this is the resolver
      fallback (`view → queryset → model → list route`) that a client-side URL match would lose.
- [ ] Accept the semantic shift: the extracted resolver tests against the *unfiltered* registry, so
      it can name an item the user cannot see. The client's selector then matches nothing and no
      class is applied — identical to today, where the item is not rendered at all. Worth a test,
      because it is obvious now and will not be in a year.

### Phase 4 — the cache

- [ ] Store gzipped (7 KB against 267 KB, so compression is free).
- [ ] Django cache backend (Redis is already in the stack). A TTL is not needed for correctness
      since the key changes when inputs change, but set one to bound memory.
- [ ] Settings flag to disable, so it can be switched off in production without a deploy.
- [ ] Test: the cached fragment contains **no** `nb-sidenav-link-active` and **no** favourite
      `active` — nothing per-request leaked into a stored entry. Cheap, and catches the worst
      regression directly.

### Phase 5 — measure

- [ ] A/B behind the flag, three alternating rounds, controls that render no chrome
      (`ui.device.list.rows`, any `api.*`).
- [ ] Record prediction against result. **The prediction is −18 ms per chrome-bearing request**
      (20.4 ms removed, ~1.5 ms fingerprint and cache read added), uncertainty ±2 ms: the Redis read
      and gzip decompress are assumed at ~0.3 ms and unmeasured, and a hit also skips the
      structure-building half of the build, which could make it better than 18 ms.

## Projected improvement

Computed by applying −18 ms to the 28 chrome-bearing scenarios of the 57-scenario read loop, from
measured per-scenario Tier 2 medians:

    ui.chrome.404          56 ->  38 ms   -32.1%
    ui.status.list        118 -> 100 ms   -15.3%
    ui.home               173 -> 155 ms   -10.4%
    ui.device.list        219 -> 201 ms    -8.2%
    ui.device.interfaces  631 -> 613 ms    -2.9%

    read loop aggregate   20,777 -> 20,273 ms   (-2.4%)
    full device-list view     822 ->    804 ms   (-2.2%)

**The two headline numbers disagree and both are true.** A typical UI page gets 8–16% faster and
the cheapest chrome-bearing pages up to −32%, while the workload aggregate moves only −2.4% —
because 29 of 57 scenarios render no chrome, and a fixed 18 ms is a third of a 56 ms page and 3% of
a 631 ms one. This is the shape of change that makes an app feel quicker everywhere without moving
a benchmark total.

## Risk

**B2 (state outliving request scope) + security-visible.** Finding 51's docstring is the reason to
be careful: it documents that its cache is request-scoped *specifically* so "two users cannot
observe each other's menu." This change gives that property up deliberately. The `user.pk` prefix
restores it structurally, which is why it is preferred over relying on fingerprint exactness — a
subtle collision serving the wrong navigation is the failure mode least worth risking.

## What this does not fix

**The response is still 401 KB with 267 KB of menu markup.** Caching removes CPU, not bytes, and
gzip already handles the wire cost. Winning the payload back means rendering the menu client-side —
set aside because apps currently contribute *markup* to the menu, not just data, so that path needs
an app-extension contract first. `register_menu_items`' own docstring agrees it is due a
refactor: *"This is almost certainly overcomplicated and could do with significant refactoring at
some point."*
