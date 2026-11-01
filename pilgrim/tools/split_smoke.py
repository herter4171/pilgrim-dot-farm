"""Local split smoke checks (SPLIT_PLAN §7.3).

Runs against the loopback stack from `pilgrim/deploy/local/stack.sh` in
`real` mode: the REAL public app (`pilgrim.tests.e2e.split_origin`) behind the
gateway's PRODUCTION allowlist and the edge. Every check prints PASS/FAIL; the
process exits non-zero if any FAIL. No real backends are contacted.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.server
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
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


def _origin_cmd(state: Path, port: int) -> list[str]:
    """Restart the same origin stack.sh started (its mode is recorded)."""
    mode = (state / "origin_mode").read_text().strip()
    if mode == "real":
        return [sys.executable, "-m", "pilgrim.tests.e2e.split_origin",
                "--port", str(port), "--state-dir", str(state / "origin")]
    return [sys.executable, "-m", "uvicorn", "pilgrim.tests.e2e.e2e_server:app",
            "--host", "127.0.0.1", "--port", str(port)]


def _visitor_ips(state: Path) -> list[str]:
    """Read the real origin's temp DB directly: no test route on the public app."""
    con = sqlite3.connect(f"file:{state / 'origin' / 'station.db'}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT ip FROM visitor_ips ORDER BY ip")]
    finally:
        con.close()


def _start_origin(state: Path, port: int) -> None:
    log = open(state / "origin.log", "ab")  # noqa: SIM115 - held by the child
    proc = subprocess.Popen(_origin_cmd(state, port),
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
    raise RuntimeError("origin did not restart")


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


class _Bare502(http.server.BaseHTTPRequestHandler):
    """Upstream-sent bare 502, like Tailscale serve when the gateway is down."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


def _start_bare502(port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Bare502)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _json_or_none(response: httpx.Response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


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
        # admin routes, and (prod allowlist) the e2e-only /api/test/ routes
        codes = {f"{name}:{path}": client.get(path).status_code
                 for path in ("/api/admin/dj/state", "/api/test/report",
                              "/api/admin/payments")
                 for name, client in (("edge", edge), ("gateway", gateway))}
        return all(c == 404 for c in codes.values()), f"{codes}"

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
                      headers={"X-Real-IP": "203.0.113.9",
                               "X-Forwarded-For": "203.0.113.10"})
        ips = _visitor_ips(state)
        ok = (r.status_code == 200 and ips == ["127.0.0.1"])
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

    def check_9_upstream_502():
        """SPLIT_PLAN Step 10.2: serve sends a bare 502 when the gateway is
        down while tailscaled is up. The edge must intercept it (error_page
        502/504) and answer with the off-air bodies, not pass the empty body
        through. The local stack has no serve hop, so stub the 502 here."""
        gw_port = httpx.URL(gateway_url).port or 80
        stub = _start_bare502(gw_port)
        try:
            off = _poll_on_air(edge, False, 5.0)
            p503 = _poll_program_off_air(edge, 5.0)
            h = edge.get("/api/health")
            p = edge.get("/api/station/program")
            e = edge.get("/edge-health")
            hb, pb = _json_or_none(h), _json_or_none(p)
            ok = (off and p503
                  and h.status_code == 200 and hb is not None
                  and hb.get("on_air") is False
                  and p.status_code == 503 and pb is not None
                  and pb.get("off_air") is True
                  and e.status_code == 200)
            return ok, (f"health={h.status_code}/{hb} "
                        f"program={p.status_code}/{pb} "
                        f"edge-health={e.status_code}")
        finally:
            stub.shutdown()
            stub.server_close()

    record("9d upstream-sent bare 502 is intercepted", check_9_upstream_502)

    def check_9_recover_gateway():
        _compose(state, "start", "gateway")
        return _poll_on_air(edge, True, 5.0), "on_air true within 5 s"

    record("9e recovery after gateway restart", check_9_recover_gateway)

    def one_request() -> tuple[int, bool]:
        with httpx.Client(base_url=gateway_url, timeout=15.0) as c:
            r = c.post("/api/requests", json={"text": "play polka"})
            # nginx's limit_req page is HTML; the origin's own 4.3 limiter
            # answers JSON. Only the gateway's 429s count here.
            return r.status_code, "json" in r.headers.get("content-type", "")

    def check_10():
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
            res = list(ex.map(lambda _: one_request(), range(30)))
        gw429 = sum(1 for code, is_json in res if code == 429 and not is_json)
        origin429 = sum(1 for code, is_json in res if code == 429 and is_json)
        board = gateway.get("/api/requests").status_code  # GET is never limited
        return (gw429 >= 1 and board == 200,
                f"gateway 429s={gw429} origin 429s={origin429} board GET={board}")

    if max_check >= 10:
        record("10 gateway rate limit: POST 429s from nginx, GET board unlimited", check_10)

    if max_check >= 11:
        def check_11():
            r = edge.post("/api/stripe/webhook", content=b"{}",
                          headers={"stripe-signature": "t=1,v1=deadbeef",
                                   "content-type": "application/json"})
            # Payments are disabled in the local stack, so the route answers
            # 503 "payments disabled" (not 404): the path is open end-to-end.
            return (r.status_code == 503 and "payments" in r.text.lower(),
                    f"{r.status_code} {r.text[:60]!r}")

        def check_12():
            r = edge.get("/api/paid-requests/" + "A" * 24)
            return r.status_code == 404, f"{r.status_code}"

        def check_13():
            r = edge.get("/api/paid-requests/x")
            return r.status_code == 404, f"{r.status_code}"

        def check_14():
            r = edge.post("/api/paid-requests", json={"text": "a paid song"})
            is_json = r.headers.get("content-type", "").startswith("application/json")
            body = r.json() if is_json else {}
            return (r.status_code == 503 and body.get("paid_closed") is True
                    and "off_air" not in body,
                    f"{r.status_code} {body}")

        def check_15():
            r = edge.post("/api/paid-requests/" + "A" * 24 + "/checkout")
            is_json = r.headers.get("content-type", "").startswith("application/json")
            body = r.json() if is_json else {}
            # route open end to end; payments disabled locally -> paid_closed
            return (r.status_code == 503 and body.get("paid_closed") is True,
                    f"{r.status_code} {body}")

        record("11 webhook path open, payments disabled -> 503", check_11)
        record("12 paid status unknown token -> 404", check_12)
        record("13 malformed paid token -> 404 at edge", check_13)
        record("14 paid request closed -> 503 paid_closed", check_14)
        record("15 paid checkout path open, payments disabled -> 503", check_15)

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
    ap.add_argument("--max-check", type=int, default=10, choices=range(1, 16),
                    metavar="1..15",
                    help="stop after this check (production cutover uses 7)")
    args = ap.parse_args(argv)
    return run(args.edge, args.gateway, args.state_dir, args.max_check)


if __name__ == "__main__":
    sys.exit(main())
