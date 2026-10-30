"""Local split smoke checks (SPLIT_PLAN §7.3).

Runs against the loopback stack from `pilgrim/deploy/local/stack.sh`
(e2e origin + gateway + edge). Every check prints PASS/FAIL; the process exits
non-zero if any FAIL. No real backends are contacted.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "pilgrim" / "deploy" / "local" / "compose.yaml"
PROJECT = "pilgrim-split-local"


def _port_closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_port_closed(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_closed(port):
            return True
        time.sleep(0.1)
    return False


def _start_origin(state: Path, port: int) -> None:
    log = open(state / "origin.log", "ab")  # noqa: SIM115 - held by the child
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "pilgrim.tests.e2e.e2e_server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    (state / "origin.pid").write_text(str(proc.pid))
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health",
                         timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("e2e origin did not restart")


def _compose(state: Path, *args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE),
         "--env-file", str(state / "env"), "-p", PROJECT, *args],
        cwd=ROOT, check=True, env=dict(os.environ))


def _poll_on_air(client: httpx.Client, want: bool, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get("/api/health")
            if r.status_code == 200 and bool(r.json().get("on_air")) == want:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.2)
    return False


def _poll_program_off_air(client: httpx.Client, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get("/api/station/program")
            if r.status_code == 503 and r.json().get("off_air") is True:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.2)
    return False


def run(edge_url: str, gateway_url: str, state: Path,
        max_check: int = 10) -> int:
    edge = httpx.Client(base_url=edge_url, timeout=15.0)
    gateway = httpx.Client(base_url=gateway_url, timeout=15.0)
    results: list[bool] = []

    def record(label: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 - a failed check is data
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}"
              + (f"  [{detail}]" if detail else ""))

    def check_1():
        r = edge.get("/edge-health")
        return (r.status_code == 200 and r.text.strip() == "ok",
                f"{r.status_code} {r.text.strip()!r}")

    def check_2():
        r = edge.get("/")
        return (r.status_code == 200 and "PILGRIM" in r.text,
                f"{r.status_code}")

    def check_3():
        a = edge.get("/api/admin/dj/state")
        b = gateway.get("/api/admin/dj/state")
        return (a.status_code == 404 and b.status_code == 404,
                f"edge={a.status_code} gateway={b.status_code}")

    def check_4():
        codes = []
        for method in ("GET", "POST"):
            for client in (edge, gateway):
                codes.append(client.request(method, "/api/station/start").status_code)
        return (all(c == 404 for c in codes), f"{codes}")

    program: dict = {}

    def check_5():
        deadline = time.time() + 5.0
        r = None
        while time.time() < deadline:
            r = edge.get("/api/station/program")
            if r.status_code == 200:
                body = r.json()
                if body.get("items"):
                    program.update(body)
                    return True, f"epoch={body.get('epoch')} items={len(body['items'])}"
            time.sleep(0.2)
        return False, f"{getattr(r, 'status_code', '?')}"

    def check_6():
        media_id = program["items"][0]["media_id"]
        r1 = edge.get(f"/api/media/{media_id}")
        r2 = edge.get(f"/api/media/{media_id}")
        h1 = hashlib.sha256(r1.content).hexdigest()
        h2 = hashlib.sha256(r2.content).hexdigest()
        ok = (r1.status_code == 200 and r2.status_code == 200
              and r1.headers.get("x-cache-status") == "MISS"
              and r2.headers.get("x-cache-status") == "HIT"
              and h1 == h2
              and "immutable" in (r1.headers.get("cache-control") or ""))
        return ok, (f"id={media_id} {r1.headers.get('x-cache-status')}/"
                    f"{r2.headers.get('x-cache-status')} "
                    f"{r1.headers.get('cache-control')}")

    def check_7():
        r = edge.post("/api/requests", json={"text": "play some polka"})
        ctype = r.headers.get("content-type", "")
        body = r.json()
        return (r.status_code == 200 and "application/json" in ctype
                and "ok" in body,
                f"{r.status_code} {ctype}")

    def check_8():
        r = edge.post("/api/visitors",
                      headers={"X-Real-IP": "203.0.113.9"})
        rep = edge.get("/api/test/report").json()
        ips = rep.get("visitor_ips", [])
        ok = (r.status_code == 200 and "127.0.0.1" in ips
              and "203.0.113.9" not in ips)
        return ok, f"recorded={ips}"

    record("1 edge-health 200 ok", check_1)
    record("2 GET / serves index.html", check_2)
    record("3 admin route 404 at edge and gateway", check_3)
    record("4 station start/stop 404 at edge and gateway", check_4)
    record("5 program 200 with epoch", check_5)
    record("6 media MISS->HIT, identical bytes, immutable", check_6)
    record("7 POST /api/requests returns origin JSON", check_7)
    if max_check >= 8:
        record("8 spoofed X-Real-IP ignored (origin sees 127.0.0.1)", check_8)

    if max_check < 9:
        edge.close()
        gateway.close()
        failed = results.count(False)
        print(f"\n{len(results) - failed} passed, {failed} failed")
        return 1 if failed else 0

    origin_port = int((state / "origin_port").read_text())

    def check_9_origin():
        pid = int((state / "origin.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        _wait_port_closed(origin_port)
        # Poll past the edge micro-cache TTL instead of a fixed sleep; nginx
        # rounds max-age=2 up to about 3 s.
        off = _poll_on_air(edge, False, 5.0)
        p503 = _poll_program_off_air(edge, 5.0)
        h = edge.get("/api/health")
        p = edge.get("/api/station/program")
        e = edge.get("/edge-health")
        ok = (off and p503
              and h.status_code == 200 and h.json().get("on_air") is False
              and p.status_code == 503 and p.json().get("off_air") is True
              and e.status_code == 200)
        return ok, (f"health={h.status_code}/{h.json().get('on_air')} "
                    f"program={p.status_code}/{p.json().get('off_air')} "
                    f"edge-health={e.status_code}")

    record("9a off air with origin stopped", check_9_origin)

    def check_9_recover_origin():
        _start_origin(state, origin_port)
        return _poll_on_air(edge, True, 5.0), "on_air true within 5 s"

    record("9b recovery after origin restart", check_9_recover_origin)

    def check_9_gateway():
        _compose(state, "stop", "gateway")
        off = _poll_on_air(edge, False, 5.0)
        p503 = _poll_program_off_air(edge, 5.0)
        h = edge.get("/api/health")
        p = edge.get("/api/station/program")
        e = edge.get("/edge-health")
        ok = (off and p503
              and h.status_code == 200 and h.json().get("on_air") is False
              and p.status_code == 503 and p.json().get("off_air") is True
              and e.status_code == 200)
        return ok, (f"health={h.status_code}/{h.json().get('on_air')} "
                    f"program={p.status_code}/{p.json().get('off_air')} "
                    f"edge-health={e.status_code}")

    record("9c off air with gateway stopped", check_9_gateway)

    def check_9_recover_gateway():
        _compose(state, "start", "gateway")
        return _poll_on_air(edge, True, 5.0), "on_air true within 5 s"

    record("9d recovery after gateway restart", check_9_recover_gateway)

    def one_request() -> int:
        with httpx.Client(base_url=gateway_url, timeout=15.0) as c:
            return c.post("/api/requests", json={"text": "play polka"}).status_code

    def check_10():
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
            codes = list(ex.map(lambda _: one_request(), range(30)))
        n429 = codes.count(429)
        return n429 >= 1, f"429s={n429} statuses={sorted(set(codes))}"

    if max_check >= 10:
        record("10 gateway rate limit returns 429", check_10)

    edge.close()
    gateway.close()
    failed = results.count(False)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--edge", default="http://127.0.0.1:18080")
    ap.add_argument("--gateway", default="http://127.0.0.1:18090")
    ap.add_argument("--state-dir", type=Path,
                    default=Path("/tmp/pilgrim-split-local"))
    ap.add_argument("--max-check", type=int, default=10, choices=range(1, 11),
                    metavar="1..10",
                    help="stop after this check (production cutover uses 7)")
    args = ap.parse_args(argv)
    return run(args.edge, args.gateway, args.state_dir, args.max_check)


if __name__ == "__main__":
    sys.exit(main())
