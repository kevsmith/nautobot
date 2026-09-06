#!/usr/bin/env python
"""Sit between databot and Nautobot and keep whatever fails.

`POST /api/dcim/cables/` returns HTTP 500 on every batch of a datacenter apply,
15-17 times per run. Nautobot's exception middleware turns the traceback into a
246-byte JSON body and Django logs only "Internal Server Error", so neither the
container log nor databot's warning says what actually broke -- and the failure
has not reproduced from a payload rebuilt by hand, which means the payload
itself is the missing evidence.

This forwards every request unchanged and, on any 5xx, writes the exact request
body and response body to disk. The saved request can then be replayed
in-process, where the exception is raised rather than rendered.

    python perf/capture_proxy.py --listen 8199 --target http://localhost:8180
    NAUTOBOT_URL=http://localhost:8199 databot apply perf/large-dc-dataset.yml

Deliberately not a measurement tool: it adds a hop and serializes requests, so
nothing timed should run through it.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys
import urllib.error
import urllib.request

ARGS = None
CAPTURED = 0


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _relay(self, method):
        global CAPTURED
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        # The scheme is not attacker-controlled: `--target` is an
        # operator-supplied base URL for the Nautobot under test, and this is a
        # local diagnostic that never runs unattended -- hence the S310 waivers.
        request = urllib.request.Request(  # noqa: S310
            ARGS.target + self.path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=ARGS.timeout) as response:  # noqa: S310
                status, payload, out_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status, payload, out_headers = exc.code, exc.read(), exc.headers
        except Exception as exc:
            status, payload, out_headers = 502, str(exc).encode(), {}

        if status >= 500:
            CAPTURED += 1
            stem = os.path.join(ARGS.out, f"fail-{CAPTURED:03d}-{self.path.strip('/').replace('/', '_')}")
            if body:
                with open(f"{stem}.request.json", "wb") as fh:
                    fh.write(body)
            with open(f"{stem}.response.txt", "wb") as fh:
                fh.write(payload)
            print(f"captured {status} {method} {self.path} -> {stem}.*", file=sys.stderr, flush=True)
            print(f"    {payload[:400].decode(errors='replace')}", file=sys.stderr, flush=True)

        self.send_response(status)
        for key, value in out_headers.items() if out_headers else []:
            if key.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_PATCH(self):
        self._relay("PATCH")

    def do_PUT(self):
        self._relay("PUT")

    def do_DELETE(self):
        self._relay("DELETE")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8199)
    ap.add_argument("--target", default="http://localhost:8180")
    # Under perf/results/, which is gitignored, so captures land with the rest
    # of the run artifacts rather than somewhere the next reboot decides about.
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "capture"))
    ap.add_argument("--timeout", type=int, default=300)
    ARGS = ap.parse_args()
    os.makedirs(ARGS.out, exist_ok=True)
    print(f"proxy :{ARGS.listen} -> {ARGS.target}, captures 5xx into {ARGS.out}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.listen), Proxy).serve_forever()


if __name__ == "__main__":
    main()
