import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import core, google_drive as gd, sync


class GoogleCredentials(unittest.TestCase):
    def test_credential_roundtrip_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEAD_WORKBENCH_HOME": tmp}):
            credentials = {"refresh_token": "fictional-test-token", "client_id": "fictional-client"}
            gd.save_credentials(credentials)
            self.assertEqual(gd.load_credentials(), credentials)
            if os.name == "nt":
                self.assertNotIn(b"fictional-test-token", gd.credential_path().read_bytes())
            self.assertFalse(gd.disconnect()["connected"])
            with self.assertRaisesRegex(gd.GoogleError, "not connected"):
                gd.load_credentials()

    def test_desktop_consent_state_pkce_scope_and_separate_laptop_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEAD_WORKBENCH_HOME": tmp}):
            client = Path(tmp) / "client.json"
            client.write_text(json.dumps({"installed": {"client_id": "fictional.apps.googleusercontent.com"}}))
            auth = {}
            threads = []
            def browser(url):
                auth.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()})
                def callback():
                    # An invalid state must not complete authorization.
                    bad = auth["redirect_uri"] + "?" + urllib.parse.urlencode({"state": "wrong", "code": "bad"})
                    try:
                        urllib.request.urlopen(bad, timeout=5)
                    except urllib.error.HTTPError as exc:
                        self.assertEqual(exc.code, 400)
                        exc.close()
                    good = auth["redirect_uri"] + "?" + urllib.parse.urlencode({"state": auth["state"], "code": "fictional-code"})
                    with urllib.request.urlopen(good, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                thread = threading.Thread(target=callback)
                threads.append(thread)
                thread.start()
                return True
            def token(fields):
                challenge = base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest()).decode().rstrip("=")
                self.assertEqual(challenge, auth["code_challenge"])
                self.assertEqual(fields["code"], "fictional-code")
                return {"scope": gd.SCOPE, "refresh_token": "fictional-refresh"}
            with patch.object(gd.webbrowser, "open", side_effect=browser), patch.object(gd, "token_request", side_effect=token):
                result = gd.connect_google(client, timeout=10)
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(result["connected"])
            self.assertFalse(result["sync_enabled"])
            self.assertEqual(auth["scope"], gd.SCOPE)
            self.assertNotIn("gmail", auth["scope"])
            self.assertEqual(auth["code_challenge_method"], "S256")
            self.assertEqual(gd.load_credentials()["refresh_token"], "fictional-refresh")

    def test_no_browser_does_not_store_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LEAD_WORKBENCH_HOME": tmp}):
            client = Path(tmp) / "client.json"
            client.write_text(json.dumps({"installed": {"client_id": "fictional.apps.googleusercontent.com"}}))
            with patch.object(gd.webbrowser, "open", return_value=False), self.assertRaises(gd.GoogleError):
                gd.connect_google(client)
            self.assertFalse(gd.credential_path().exists())


class GoogleHTTP(unittest.TestCase):
    """Exercise real urllib requests against an isolated protocol fixture server."""
    def setUp(self):
        self.files = {}
        self.uploads = 0
        self.list_page_calls = 0
        self.failures = 0
        self.denied = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/token":
                    return self.respond({"access_token": "fictional-access"})
                if self.path.startswith("/upload"):
                    owner.uploads += 1
                    # Decode the multipart parts produced by the actual transport.
                    boundary = self.headers["Content-Type"].split("boundary=")[1]
                    parts = body.split(("--" + boundary).encode())
                    metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].strip())
                    revision = json.loads(parts[2].split(b"\r\n\r\n", 1)[1].strip())
                    owner.files[metadata["name"]] = revision
                    return self.respond({"id": metadata["name"]})
                metadata = json.loads(body)
                owner.files[metadata["name"]] = None
                return self.respond({"id": metadata["name"]})
            def do_GET(self):
                if owner.denied:
                    return self.respond({"error": "fictional-sensitive-server-detail"}, 401)
                if owner.failures:
                    owner.failures -= 1
                    return self.respond({}, 503)
                parsed = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if "alt" in params:
                    return self.respond(owner.files[urllib.parse.unquote(parsed.path.split("/")[-1])])
                self.assert_auth()
                query = params["q"][0]
                matching = [name for name in owner.files if (name == query.split("'")[1] if "name =" in query else gd.PREFIX in name)]
                offset = int(params.get("pageToken", [0])[0])
                batch = matching[offset:offset+1]
                owner.list_page_calls += 1
                result = {"files": [{"id": name, "name": name} for name in batch]}
                if offset + 1 < len(matching):
                    result["nextPageToken"] = str(offset + 1)
                return self.respond(result)
            def assert_auth(self):
                owner.assertEqual(self.headers.get("Authorization"), "Bearer fictional-access")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.patches = [patch.object(gd, "API", base+"/files"), patch.object(gd, "UPLOAD", base+"/upload"), patch.object(gd, "TOKEN_URL", base+"/token")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_drive_transport_multipart_pagination_and_retries(self):
        drive = gd.Drive({"client_id": "fictional", "refresh_token": "fictional"})
        self.assertTrue(drive.identity.startswith("google-drive:"))
        self.failures = 1
        with tempfile.TemporaryDirectory() as tmp:
            a = core.connect(Path(tmp) / "a.sqlite3")
            b = core.connect(Path(tmp) / "b.sqlite3")
            try:
                core.upsert_leads(a, {"linkedin_url": "https://www.linkedin.com/in/fictional-http", "name": "Fictional HTTP"}, {})
                sync.synchronize(a, drive)
                sync.synchronize(b, drive)
                before = self.uploads
                sync.synchronize(a, drive)
                self.assertEqual(self.uploads, before)
                self.assertEqual(core.list_leads(b)[0]["name"], "Fictional HTTP")
                self.assertGreater(self.list_page_calls, 3)
                self.assertEqual(self.failures, 0)
            finally:
                a.close()
                b.close()

    def test_denied_access_reports_safe_error(self):
        drive = gd.Drive({"client_id": "fictional", "refresh_token": "fictional"})
        self.denied = True
        with self.assertRaises(gd.GoogleError) as error:
            drive.list_operations()
        self.assertNotIn("fictional-sensitive", str(error.exception))


if __name__ == "__main__":
    unittest.main()
