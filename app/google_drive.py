"""Optional Drive app-data transport and desktop OAuth. Standard library only.

No Gmail permission. Credentials are never exposed through MCP tools or synced.
Windows credentials use DPAPI; POSIX credentials use owner-only files.
"""
import base64
import ctypes
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import core, sync
from .paths import data_dir

SCOPE = "https://www.googleapis.com/auth/drive.appdata"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
PREFIX = "lead-workbench-v1-"
MARKER = PREFIX + "identity"


class GoogleError(core.ValidationError):
    pass


def _dpapi(data, decrypt=False):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source, dest = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), Blob()
    protect = ctypes.windll.crypt32.CryptProtectData
    unprotect = ctypes.windll.crypt32.CryptUnprotectData
    protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = unprotect.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    if decrypt:
        ok = unprotect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        ok = protect(ctypes.byref(source), "Lead Workbench", None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise GoogleError("Windows credential protection failed")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        free(dest.data)


def credential_path():
    return data_dir() / "google-credentials.bin"


def save_credentials(value, path=None):
    path = Path(path or credential_path())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = sync.encode(value).encode("utf8")
    payload = b"DPAPI\n" + _dpapi(payload) if os.name == "nt" else b"PRIVATE\n" + payload
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_credentials(path=None):
    path = Path(path or credential_path())
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        raise GoogleError("Drive is not connected. Run google-connect first, or continue locally.") from None
    if payload.startswith(b"DPAPI\n") and os.name == "nt":
        raw = _dpapi(payload[6:], decrypt=True)
    elif payload.startswith(b"PRIVATE\n") and os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise GoogleError("Credential file permissions must be owner-only (chmod 600).")
        raw = payload[8:]
    else:
        raise GoogleError("Credentials cannot be used on this computer; connect Google separately on each laptop.")
    return json.loads(raw)


def request(url, *, method="GET", body=None, headers=None, attempts=3):
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise GoogleError("Google response exceeded size limit")
                try:
                    return json.loads(raw) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    raise GoogleError("Google returned invalid JSON; retry later. Local changes remain available.") from None
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(0.25 * 2**attempt)
                continue
            if exc.code in (400, 401, 403):
                raise GoogleError("Google denied the request. Check consent, API setup or reconnect Google; local data is unchanged.") from None
            raise GoogleError(f"Google request failed (HTTP {exc.code}); retry sync later.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < attempts - 1:
                time.sleep(0.25 * 2**attempt)
                continue
            raise GoogleError("Google is unreachable; local changes remain available. Retry when online.") from None
    raise GoogleError("Google request failed")


def token_request(fields):
    return request(TOKEN_URL, method="POST", body=urllib.parse.urlencode(fields).encode("ascii"),
                   headers={"Content-Type": "application/x-www-form-urlencoded"})


def connect_google(client_path, *, timeout=180):
    """User-invoked only. Browser consent + loopback callback with state and PKCE."""
    client = json.loads(Path(client_path).read_text(encoding="utf-8-sig")).get("installed", {})
    client_id = client.get("client_id")
    if not client_id or not client_id.endswith(".apps.googleusercontent.com"):
        raise GoogleError("Use an OAuth Desktop app client JSON downloaded from Google Cloud.")
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    result = {}
    class Callback(BaseHTTPRequestHandler):
        timeout = 5
        def log_message(self, *args):
            pass
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            valid = parsed.path == "/oauth/callback" and secrets.compare_digest(values.get("state", [""])[0], state)
            if valid:
                result.update({k: v[0] for k, v in values.items()})
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"You can close this tab and return to Lead Workbench." if valid else b"Invalid callback.")
    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect,
            "response_type": "code", "scope": SCOPE, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account"})
        if not webbrowser.open(url):
            raise GoogleError("Could not open a browser. Configure your default browser and try again.")
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in result:
        raise GoogleError("Google connection was cancelled or timed out. Local mode remains available.")
    tokens = token_request({"client_id": client_id, "client_secret": client.get("client_secret", ""),
                           "code": result["code"], "code_verifier": verifier,
                           "redirect_uri": redirect, "grant_type": "authorization_code"})
    granted = set(tokens.get("scope", "").split())
    if SCOPE not in granted or not tokens.get("refresh_token"):
        raise GoogleError("Google did not grant persistent Drive app-data access; connect again.")
    credentials = {"client_id": client_id, "client_secret": client.get("client_secret", ""),
                   "refresh_token": tokens["refresh_token"]}
    save_credentials(credentials)
    return {"connected": True, "scope": SCOPE, "sync_enabled": False,
            "next": "Run sync-google to upload this workbench to the selected Google account."}


class Drive:
    def __init__(self, credentials=None):
        credentials = credentials if credentials is not None else load_credentials()
        token = token_request({"client_id": credentials["client_id"],
                               "client_secret": credentials.get("client_secret", ""),
                               "refresh_token": credentials["refresh_token"], "grant_type": "refresh_token"})
        self.headers = {"Authorization": "Bearer " + token["access_token"]}
        markers = self._list("name = '" + MARKER + "'")
        if not markers:
            marker = request(API, method="POST", body=sync.encode({"name": MARKER, "parents": ["appDataFolder"]}).encode("utf8"),
                             headers=dict(self.headers, **{"Content-Type": "application/json"}))
            markers = [marker]
        # Concurrent first connections can make multiple markers. Stable minimum
        # converges; a marker change only causes harmless idempotent re-uploads.
        self.identity = "google-drive:" + min(m["id"] for m in markers)

    def _list(self, query):
        files, page = [], None
        while True:
            params = {"spaces": "appDataFolder", "q": query, "fields": "nextPageToken,files(id,name,size)", "pageSize": 1000}
            if page:
                params["pageToken"] = page
            result = request(API + "?" + urllib.parse.urlencode(params), headers=self.headers)
            files.extend(result.get("files", []))
            page = result.get("nextPageToken")
            if not page:
                return files

    def put(self, ident, revision):
        name = PREFIX + ident + ".json"
        # Retry after a lost create response may yield duplicate Drive files.
        # Content IDs make these harmless; the reader verifies and deduplicates.
        if self._list("name = '" + name + "'"):
            return
        boundary = "workbench_" + secrets.token_hex(12)
        metadata = sync.encode({"name": name, "parents": ["appDataFolder"]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
                f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{sync.encode(revision)}\r\n--{boundary}--\r\n").encode("utf8")
        request(UPLOAD + "?uploadType=multipart&fields=id", method="POST", body=body,
                headers=dict(self.headers, **{"Content-Type": "multipart/related; boundary=" + boundary}))

    def list_operations(self):
        result = {}
        for file in self._list("name contains '" + PREFIX + "'"):
            name = file["name"]
            if name == MARKER:
                continue
            ident = name.removeprefix(PREFIX).removesuffix(".json")
            if len(ident) != 64 or any(c not in "0123456789abcdef" for c in ident):
                continue
            if int(file.get("size", 0)) > sync.MAX_OPERATION_BYTES:
                raise GoogleError("A Drive revision exceeds the supported size")
            revision = request(API + "/" + urllib.parse.quote(file["id"], safe="") + "?alt=media", headers=self.headers)
            if revision.get("id") != ident:
                raise GoogleError("Drive revision name does not match its content")
            result[ident] = revision
        return list(result.values())


def disconnect():
    # This removes local access only; Google Account permissions can be revoked
    # separately. Remote revisions are retained until explicitly erased by user.
    path = credential_path()
    path.unlink(missing_ok=True)
    return {"connected": False, "remote_data_deleted": False}
