#!/usr/bin/env python
"""Dump {job name: task_queues} so the two arms can be compared without a response digest.

`probe_endpoint_ab.py`'s body digest is useless on `/api/extras/jobs/`: Nautobot re-registers its
Jobs at startup, rewriting `extras_job.last_updated`, and every arm swap restarts the container --
so the digest differs each round on both arms for a reason that has nothing to do with any change
under test. This dumps only the field the change can affect.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_jobs_equivalence.py
"""

import json
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

client = get_perf_client()
results = json.loads(client.get("/api/extras/jobs/?limit=100").content)["results"]
print(json.dumps({job["name"]: job["task_queues"] for job in results}, sort_keys=True))
