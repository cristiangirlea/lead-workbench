"""Validate real local clients in isolated profiles, without an AI turn or Google account."""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def run(command, env, cwd):
    result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True,
                            encoding="utf8", timeout=40)
    if result.returncode:
        raise RuntimeError("Client command failed: " + " ".join(command[:3]) + "\n" + result.stderr)
    return result.stdout


def codex_tools(env, cwd):
    with tempfile.TemporaryFile(mode="w+", encoding="utf8") as errors:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, text=True, encoding="utf8", env=env, cwd=cwd)
        messages = queue.Queue()
        def read():
            for line in proc.stdout:
                messages.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def rpc(ident, method, params):
            proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while True:
                value = messages.get(timeout=30)
                if value.get("id") == ident:
                    if "error" in value:
                        raise RuntimeError(str(value["error"]))
                    return value["result"]
        try:
            rpc(0, "initialize", {"clientInfo": {"name": "workbench_validation", "version": "0.2.0"},
                                   "capabilities": {"experimentalApi": True}})
            proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            proc.stdin.flush()
            result = rpc(1, "mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})
            server = next(item for item in result["data"] if item.get("pluginId") == "lead-workbench@lead-workbench-local")
            if server.get("toolsError") or not server.get("serverCapabilities") or not server.get("tools"):
                raise RuntimeError("Codex MCP startup failed: " + str(server.get("toolsError")))
            return {"connected": True, "tools": sorted(server["tools"])}
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
            reader.join(timeout=5)
            proc.stdout.close()


def validate(package):
    result = {}
    for client in ("codex", "claude"):
        if not shutil.which(client):
            raise RuntimeError(client + " is not installed")
        # No normal host config/auth or databases are copied into this profile.
        with tempfile.TemporaryDirectory(prefix="lead-workbench-" + client + "-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            env = dict(os.environ, LEAD_WORKBENCH_HOME=str(root / "data"))
            env.pop("LNAPP_DB", None)
            (root / "profile").mkdir()
            env["CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR"] = str(root / "profile")
            if client == "codex":
                run(["codex", "plugin", "marketplace", "add", str(package), "--json"], env, workspace)
                run(["codex", "plugin", "add", "lead-workbench@lead-workbench-local", "--json"], env, workspace)
                installed = json.loads(run(["codex", "plugin", "list", "--json"], env, workspace))["installed"]
                assert any(p["name"] == "lead-workbench" and p["enabled"] for p in installed)
                result[client] = {"installed": True, **codex_tools(env, workspace)}
            else:
                run(["claude", "plugin", "marketplace", "add", str(package)], env, workspace)
                run(["claude", "plugin", "install", "lead-workbench@lead-workbench-local"], env, workspace)
                installed = json.loads(run(["claude", "plugin", "list", "--json"], env, workspace))
                assert any(p["id"] == "lead-workbench@lead-workbench-local" and p["enabled"] for p in installed)
                health = run(["claude", "mcp", "list"], env, workspace)
                connected = any("lead-workbench" in line and "Connected" in line for line in health.splitlines())
                if not connected:
                    raise RuntimeError("Claude MCP did not connect: " + health)
                result[client] = {"installed": True, "connected": True}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf8")
    print(json.dumps(validate(args.package.resolve()), indent=2))
