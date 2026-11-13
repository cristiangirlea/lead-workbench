import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import core, mcp_server

ROOT = Path(__file__).resolve().parents[1]


class Plugin(unittest.TestCase):
    def test_stdio_from_unrelated_working_directory_and_shared_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, LEAD_WORKBENCH_HOME=tmp)
            env.pop("LNAPP_DB", None)
            def run(messages):
                proc = subprocess.run([sys.executable, str(ROOT / "plugin_server.py")],
                    input="\n".join(json.dumps(m) for m in messages) + "\n", text=True,
                    encoding="utf8", capture_output=True, cwd=tmp, env=env, timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                return [json.loads(line) for line in proc.stdout.splitlines()]
            result = run([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "save_leads", "arguments": {
                    "records": [{"linkedin_url": "https://www.linkedin.com/in/fictional-plugin-test", "name": "Fictional test"}]}}},
            ])
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0]["result"]["protocolVersion"], "2025-03-26")
            self.assertGreaterEqual(len(result[1]["result"]["tools"]), 9)
            self.assertFalse(result[2]["result"]["isError"])
            # A separate process (the other assistant) reads the same user data.
            result = run([{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "find_leads"}}])
            records = json.loads(result[0]["result"]["content"][0]["text"])
            self.assertEqual(records[0]["name"], "Fictional test")
            self.assertTrue((Path(tmp) / "leads.sqlite3").exists())

    def test_tool_arguments_and_unknown_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            wb = mcp_server.Workbench(Path(tmp) / "db.sqlite3", core.load_settings(core.APP_DIR / "settings.example.json"))
            try:
                for args in ({"limit": True}, {"limit": 201}, {"surprise": "x"}, {"query": []}):
                    with self.assertRaises(core.ValidationError):
                        wb.call("find_leads", args)
                with self.assertRaises(core.ValidationError):
                    wb.call("save_leads", {"records": []})
                result = mcp_server.response(wb, {"jsonrpc": "2.0", "id": 7, "method": "unknown"})
                self.assertEqual(result["error"]["code"], -32601)
            finally:
                wb.conn.close()

    def test_local_operations_never_open_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")):
            wb = mcp_server.Workbench(Path(tmp) / "db.sqlite3", {})
            try:
                wb.call("save_leads", {"records": [{"linkedin_url": "https://www.linkedin.com/in/fictional-offline"}]})
                self.assertEqual(len(wb.call("find_leads", {})), 1)
            finally:
                wb.conn.close()

    def test_manifests_share_backend_and_do_not_bundle_data(self):
        portable = json.loads((ROOT / "plugin.json").read_text())
        claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(portable["name"], claude["name"])
        self.assertEqual(portable["version"], claude["version"])
        for name in ("mcp.json", ".mcp.json"):
            spec = json.loads((ROOT / name).read_text())["mcpServers"]["lead-workbench"]
            root_var = "PLUGIN_ROOT" if name == "mcp.json" else "CLAUDE_PLUGIN_ROOT"
            self.assertEqual(spec["args"], ["-B", "${" + root_var + "}/plugin_server.py"])
            self.assertNotIn("env", spec)

    def test_allowlisted_package_excludes_working_databases_and_private_settings(self):
        from scripts.package_plugin import package
        with tempfile.TemporaryDirectory() as tmp:
            target = package(Path(tmp) / "plugin")
            files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
            self.assertIn("app/sync.py", files)
            self.assertIn("app/google_drive.py", files)
            self.assertIn("app/settings.example.json", files)
            self.assertNotIn("app/settings.json", files)
            for name in files:
                self.assertFalse(name.startswith(("local/", "app/data/", ".git/", "output/")), name)
                self.assertFalse(name.endswith((".sqlite3", ".bin", ".pyc", ".log")), name)
            with self.assertRaises(FileExistsError):
                package(target)


if __name__ == "__main__":
    unittest.main()
