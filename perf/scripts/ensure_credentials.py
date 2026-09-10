#!/usr/bin/env python
"""Ensure the perf user, its API token, and a fixed-key session exist.

Run after anything that replaces the database. Tier 2 (`tier2_latency.py`) needs a **session
cookie** for UI scenarios -- an API token authenticates DRF only and 403s on UI endpoints -- and a
session lives in `django_session`, so it does not survive `restore_snapshot.sh` or
`arm_control.sh reset`.

That has already cost one void run: a Tier 2 A/B completed cleanly having timed exactly one of
eight scenarios, because the session had been minted before an overnight restore. The probe was
right to skip the rest (it refuses to time an endpoint returning 302 rather than report a fast
redirect), but the run looked like it had worked.

**The session is created here rather than baked into the snapshot on purpose.** A session row
carries an `expire_date`, so a snapshot-embedded session would expire two weeks after the snapshot
was taken and reintroduce the same silent failure, just later. Minting on restore gives a fresh
expiry every time. The token has no expiry, which is why that half has always survived.

The key is fixed so scripts can hardcode it. This is the same posture as the well-known dev API
token the harness already relies on, on a box whose whole purpose is measurement -- but note the
app is published on 0.0.0.0:8080, so both are LAN-reachable superuser credentials.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/ensure_credentials.py
"""

import datetime
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402
from django.contrib.sessions.models import Session  # noqa: E402
from django.utils import timezone  # noqa: E402

from nautobot.users.models import Token  # noqa: E402

PERF_USER = os.environ.get("PERF_USER", "perfbot")
PERF_SESSION_KEY = os.environ.get("PERF_SESSION_KEY", "perfbotperfbotperfbotperfbot0000")
PERF_TOKEN = os.environ.get("PERF_APPLY_TOKEN", "0123456789abcdef0123456789abcdef01234567")
SESSION_DAYS = 30


def main():
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=PERF_USER,
        defaults={"is_superuser": True, "is_staff": True, "is_active": True},
    )
    if not (user.is_superuser and user.is_staff and user.is_active):
        user.is_superuser = user.is_staff = user.is_active = True
        user.save()

    # Reuse the dev token whoever owns it: what matters for a measurement is that both arms
    # authenticate identically, not which account the API scenarios run as.
    token = Token.objects.filter(key=PERF_TOKEN).first()
    if token is None:
        token = Token.objects.create(user=user, key=PERF_TOKEN, write_enabled=True)

    store = SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
    store["_auth_user_hash"] = user.get_session_auth_hash()
    Session.objects.update_or_create(
        session_key=PERF_SESSION_KEY,
        defaults={
            "session_data": store.encode(dict(store.items())),
            "expire_date": timezone.now() + datetime.timedelta(days=SESSION_DAYS),
        },
    )

    print(f"user      {user.username} (pk={user.pk}, created={created})")
    print(f"token     {token.key} (owner={token.user.username})")
    print(f"session   {PERF_SESSION_KEY} (expires in {SESSION_DAYS}d)")


if __name__ == "__main__":
    main()
