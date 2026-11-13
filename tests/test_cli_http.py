import http.client
import io
import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

from app import cli, core, server

P = "https://www.linkedin.com/in/jane-doe"


def run_cli(db, *args, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    import sys
    old = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(["--db", db, *args])
    finally:
        sys.stdin = old
    return code or 0, out.getvalue(), err.getvalue()


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "c.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_from_file_and_stdin(self):
        path = os.path.join(self.tmp.name, "leads.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"linkedin_url": "https://uk.linkedin.com/in/Jane-Doe?trk=x", "name": "Jane"},
                       {"linkedin_url": "https://www.linkedin.com/in/bob", "name": "Bob"}], fh)
        code, out, _ = run_cli(self.db, "upsert", path)
        self.assertEqual(code, 0)
        self.assertEqual([r["action"] for r in json.loads(out)["results"]], ["created", "created"])
        code, out, _ = run_cli(self.db, "upsert", "-", stdin=json.dumps({"linkedin_url": P, "company": "Acme"}))
        self.assertEqual(json.loads(out)["results"][0]["action"], "updated")
        code, out, _ = run_cli(self.db, "list", "--q", "acme")
        rows = json.loads(out)
        self.assertEqual([(r["linkedin_url"], r["name"]) for r in rows], [(P, "Jane")])

    def test_utf8_round_trip_through_ansi_pipes(self):
        import sys
        name = "Ștefan Țurcanu"
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({"linkedin_url": P, "name": name}, ensure_ascii=False)
                                            .encode("utf-8")), encoding="cp1252")
        old_in, old_out = sys.stdin, sys.stdout
        try:
            sys.stdin = stdin
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            self.assertEqual(cli.run(["--db", self.db, "upsert"]), 0)
            out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            sys.stdout = out
            self.assertEqual(cli.run(["--db", self.db, "list"]), 0)
            out.flush()
            rows = json.loads(out.buffer.getvalue().decode("utf-8"))
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertEqual(rows[0]["name"], name)

    def test_infinity_and_missing_file(self):
        code, _, err = run_cli(self.db, "upsert", "-", stdin='{"linkedin_url": "%s", "budget_min": Infinity}' % P)
        self.assertEqual(code, 2)
        code, _, err = run_cli(self.db, "upsert", os.path.join(self.tmp.name, "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(err))
        ansi = os.path.join(self.tmp.name, "ansi.json")
        with open(ansi, "wb") as fh:
            fh.write(json.dumps({"linkedin_url": P, "name": "ştefan"}, ensure_ascii=False).encode("cp1250"))
        code, _, err = run_cli(self.db, "upsert", ansi)
        self.assertEqual(code, 2)
        self.assertIn("not UTF-8", json.loads(err)["error"])

    def test_bad_input_exit_codes(self):
        code, _, err = run_cli(self.db, "upsert", "-", stdin="{not json")
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", json.loads(err)["error"])
        code, _, err = run_cli(self.db, "update-status", "99", "qualified", "--evidence", "x")
        self.assertEqual(code, 3)

    def test_status_queue_stats_export_runs(self):
        run_cli(self.db, "upsert", "-", stdin=json.dumps({
            "linkedin_url": P, "name": "=HYPERLINK(1)", "title": "+cmd", "company": "-2+3", "notes": "@SUM(1)", "contact_country": "Netherlands", "eligibility": "worldwide",
            "eligibility_source_url": "https://acme.example/jobs", "invitation_draft": "Hi"}))
        code, out, _ = run_cli(self.db, "update-status", P, "qualified", "--evidence", "Policy checks passed")
        self.assertEqual(json.loads(out)["status"], ["research", "qualified"])
        _, out, _ = run_cli(self.db, "queue")
        self.assertEqual(len(json.loads(out)["ready"]), 1)
        _, out, _ = run_cli(self.db, "stats")
        self.assertEqual(json.loads(out)["by_status"]["qualified"], 1)
        csv_path = os.path.join(self.tmp.name, "out.csv")
        code, _, _ = run_cli(self.db, "export", "--format", "csv", "--out", csv_path)
        self.assertEqual(code, 0)
        with open(csv_path, encoding="utf-8") as fh:
            text = fh.read()
        for cell in ("'=HYPERLINK(1)", "'+cmd", "'-2+3", "'@SUM(1)"):  # formula injection neutralised
            self.assertIn(cell, text)
        with open(csv_path, encoding="utf-8") as fh:
            before = fh.read()
        code, _, err = run_cli(self.db, "export", "--out", csv_path)  # export never overwrites
        self.assertEqual(code, 2)
        self.assertIn("already exists", json.loads(err)["error"])
        with open(csv_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        _, out, _ = run_cli(self.db, "record-run", "-", stdin=json.dumps({"kind": "research", "summary": "test"}))
        self.assertEqual(json.loads(out)["id"], 1)
        _, out, _ = run_cli(self.db, "get", "1")
        self.assertEqual([e["kind"] for e in json.loads(out)["events"]], ["created", "status"])


class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd, cls.app = server.make_server(os.path.join(cls.tmp.name, "h.sqlite3"), core.load_settings(), 0)
        cls.port = cls.app.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        core.upsert_leads(cls.app.conn, {"linkedin_url": P, "name": "<script>alert(1)</script>"}, cls.app.settings)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.conn.close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=hdrs)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res.status, res.getheader("Content-Security-Policy"), payload

    def good(self, **extra):
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.app.token,
             "Origin": f"http://127.0.0.1:{self.port}"}
        h.update(extra)
        return h

    def test_binds_loopback(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_index_has_token_and_csp(self):
        status, csp, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", csp)
        self.assertIn(self.app.token, body.decode())
        self.assertNotRegex(body.decode(), r"https?://(?!www\.linkedin\.com/in/)")  # no remote assets

    def test_json_api_returns_data_not_html(self):
        status, _, body = self.req("GET", "/api/leads")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["name"], "<script>alert(1)</script>")

    def test_mutation_without_token_refused(self):
        h = self.good()
        del h["X-CSRF-Token"]
        status, _, _ = self.req("POST", "/api/leads/1/status", {"status": "qualified", "evidence": "x"}, h)
        self.assertEqual(status, 403)

    def test_wrong_token_cross_origin_bad_type_refused(self):
        body = {"status": "qualified", "evidence": "x"}
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(**{"X-CSRF-Token": "nope"}))[0], 403)
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(**{"Content-Type": "text/plain"}))[0], 403)
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(**{"Sec-Fetch-Site": "cross-site"}))[0], 403)

    def test_origin_null_and_same_site_refused(self):
        body = {"status": "qualified", "evidence": "x"}
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(Origin="null"))[0], 403)
        self.assertEqual(self.req("POST", "/api/leads/1/status", body, self.good(**{"Sec-Fetch-Site": "same-site"}))[0], 403)

    def test_localhost_host_and_origin_accepted(self):
        h = self.good(Host=f"localhost:{self.port}", Origin=f"http://localhost:{self.port}")
        h["Sec-Fetch-Site"] = "same-origin"
        status, _, body = self.req("PATCH", "/api/leads/1", {"notes": "via localhost"}, h)
        self.assertEqual(status, 200, body)

    def test_static_only_serves_two_files(self):
        for path in ("/static/../core.py", "/static/settings.json", "/static/index.html", "/static/%2e%2e/core.py"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_bad_bodies(self):
        status, _, _ = self.req("PATCH", "/api/applications/1", {"revisit_status": "done", "x": 1}, self.good())
        self.assertEqual(status, 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("PATCH", "/api/leads/1", body=b'{"budget_min": Infinity}',
                     headers={**self.good(), "Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        self.assertEqual(self.req("GET", "/api/leads/" + "9" * 40)[0], 404)

    def test_internal_error_is_json_500(self):
        original = server.core.stats
        server.core.stats = lambda *a: 1 / 0
        try:
            status, _, body = self.req("GET", "/api/stats")
        finally:
            server.core.stats = original
        self.assertEqual(status, 500)
        self.assertIn("internal error", json.loads(body)["error"])

    def test_dns_rebinding_host_refused(self):
        self.assertEqual(self.req("GET", "/api/leads", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.req("POST", "/api/leads/1", {"name": "x"}, self.good(Host="evil.example"))[0], 403)

    def test_valid_mutations(self):
        status, _, body = self.req("PATCH", "/api/leads/1", {"title": "Head of Engineering"}, self.good())
        self.assertEqual(status, 200, body)
        status, _, body = self.req("PATCH", "/api/leads/1", {"hiring_source_url": "javascript:alert(1)"}, self.good())
        self.assertEqual(status, 400)
        status, _, body = self.req("POST", "/api/leads", {"linkedin_url": "https://ro.linkedin.com/in/new-person/"}, self.good())
        self.assertEqual(json.loads(body)["url"], "https://www.linkedin.com/in/new-person")
        status, _, body = self.req("POST", "/api/leads", {"linkedin_url": P, "status": "sent"}, self.good())
        self.assertEqual(status, 400)
        status, _, body = self.req("GET", "/api/leads/1")
        self.assertNotEqual(json.loads(body)["status"], "sent")


if __name__ == "__main__":
    unittest.main()
