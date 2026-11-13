"""Local UI server. Binds 127.0.0.1 only; no outbound requests, no third-party assets.

Mutations need all of: a JSON body, the per-process token in X-CSRF-Token, an allowed
Host header (stops DNS rebinding) and, when the browser sends one, a same-origin Origin.
"""

import hmac
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import core

STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
MAX_BODY = 1_000_000
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self';"
                               " connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _no_constant(name):
    raise core.ValidationError(f"body contains {name}; use a finite number or null")


class App:
    def __init__(self, db_path, settings, port):
        self.conn = core.connect(db_path)
        self.lock = threading.Lock()
        self.settings = settings
        self.port = port
        self.token = secrets.token_urlsafe(32)

    @property
    def hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "leadbench"
    timeout = 10  # a stalled client cannot hold a worker forever
    sys_version = ""
    app: App = None  # set by make_server

    def log_message(self, fmt, *args):  # keep the console quiet; no request logging to disk
        pass

    # ------------------------------------------------------------ plumbing
    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def _host_ok(self):
        return self.headers.get("Host", "") in self.app.hosts

    def _mutation_ok(self):
        if not self._host_ok():
            return "host not allowed"
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.app.hosts}:
            return "cross-origin request refused"
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return "cross-site request refused"
        token = self.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token.encode(), self.app.token.encode()):
            return "missing or wrong X-CSRF-Token"
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return "Content-Type must be application/json"
        return None

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise core.ValidationError("bad Content-Length") from None
        if length <= 0 or length > MAX_BODY:
            raise core.ValidationError("body missing or too large")
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                raise core.ValidationError("body shorter than Content-Length")
            return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise core.ValidationError("body is not valid JSON") from None

    def _guard(self, fn, body_needed=False):
        try:
            body = self._body() if body_needed else None  # read outside the lock
            with self.app.lock:
                fn(body) if body_needed else fn()
        except core.ValidationError as exc:
            self._error(400, str(exc))
        except core.NotFound as exc:
            self._error(404, str(exc))
        except Exception as exc:  # report, don't drop the connection; details stay in the console
            print(f"error on {self.command} {self.path}: {exc!r}", flush=True)
            self._error(500, "internal error; see the server console")

    # ------------------------------------------------------------ routes
    def do_GET(self):
        if not self._host_ok():
            return self._error(421, "host not allowed")
        url = urlsplit(self.path)
        path, qs = url.path, parse_qs(url.query)
        if path == "/":
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__CSRF_TOKEN__", self.app.token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        m = re.fullmatch(r"/static/([a-z]+\.(?:js|css))", path)
        if m and m.group(1) in STATIC_TYPES:
            return self._send(200, (STATIC / m.group(1)).read_bytes(), STATIC_TYPES[m.group(1)])
        self._guard(lambda: self._api_get(path, qs))

    def _api_get(self, path, qs):
        app, conn, settings = self.app, self.app.conn, self.app.settings
        one = lambda k: (qs.get(k) or [None])[0]
        if path == "/api/leads":
            rows = core.list_leads(conn, status=one("status") or None, q=one("q"), limit=1000)
            for r in rows:
                r["queue_blockers"] = core.queue_blockers(r, settings)
                r["budget"] = core.budget_view(r, settings)
            if one("ready") == "1":
                rows = [r for r in rows if not r["queue_blockers"]]
            return self._send(200, rows)
        m = re.fullmatch(r"/api/leads/(\d{1,15})", path)
        if m:
            lead = core.find_lead(conn, int(m.group(1)))
            lead["queue_blockers"] = core.queue_blockers(lead, settings)
            lead["budget"] = core.budget_view(lead, settings)
            lead["events"] = core.lead_events(conn, lead["id"])
            lead["applications"] = core.company_applications(conn, lead["company_key"])
            lead["allowed_next"] = sorted(core.TRANSITIONS[lead["status"]])
            return self._send(200, lead)
        if path == "/api/applications":
            return self._send(200, core.list_applications(conn, revisit_only=one("revisit") == "1", settings=settings))
        if path == "/api/runs":
            return self._send(200, core.list_runs(conn))
        if path == "/api/stats":
            return self._send(200, core.stats(conn, settings))
        if path == "/api/policy":
            return self._send(200, settings)
        if path == "/api/meta":
            return self._send(200, {
                "statuses": core.STATUSES, "eligibility": core.ELIGIBILITY, "company_types": core.COMPANY_TYPES,
                "relationships": core.RELATIONSHIPS, "budget_periods": core.BUDGET_PERIODS,
                "revisit_statuses": core.REVISIT_STATUSES,
            })
        raise core.NotFound("no such page")

    def do_POST(self):
        self._mutate()

    def do_PATCH(self):
        self._mutate()

    def _mutate(self):
        problem = self._mutation_ok()
        if problem:
            # Read (and drop) a small body before refusing, so the client sees the 403
            # instead of a reset connection; refuse to keep the connection open.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if 0 < length <= MAX_BODY:
                self.rfile.read(length)
            self.close_connection = True
            return self._error(403, problem)
        path = urlsplit(self.path).path
        self._guard(lambda body: self._api_mutate(self.command, path, body), body_needed=True)

    def _api_mutate(self, method, path, body):
        conn, settings = self.app.conn, self.app.settings
        if method == "POST" and path == "/api/leads":
            if not isinstance(body, dict):
                raise core.ValidationError("expected one lead object")
            return self._send(200, core.upsert_leads(conn, body, settings, actor="ui")[0])
        m = re.fullmatch(r"/api/leads/(\d{1,15})", path)
        if method == "PATCH" and m:
            return self._send(200, core.edit_lead(conn, int(m.group(1)), body, settings, actor="ui"))
        m = re.fullmatch(r"/api/leads/(\d{1,15})/status", path)
        if method == "POST" and m:
            if not isinstance(body, dict):
                raise core.ValidationError("expected {status, evidence, evidence_url}")
            return self._send(200, core.update_status(
                conn, int(m.group(1)), body.get("status"), body.get("evidence"), body.get("evidence_url"), actor="ui"))
        m = re.fullmatch(r"/api/applications/(\d{1,15})", path)
        if method == "PATCH" and m:
            if not isinstance(body, dict) or set(body) != {"revisit_status"}:
                raise core.ValidationError("expected {revisit_status}")
            return self._send(200, core.set_revisit_status(conn, int(m.group(1)), body["revisit_status"], actor="ui"))
        raise core.NotFound("no such endpoint")


def make_server(db_path, settings, port):
    app = App(db_path, settings, port)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    app.port = httpd.server_address[1]
    return httpd, app


def serve(db_path, settings, port):
    httpd, app = make_server(db_path, settings, port)
    print(f"Lead workbench on http://127.0.0.1:{app.port}/  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
