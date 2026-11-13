"""Build an allowlisted plugin directory; never copy a workspace wholesale."""
import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Choose a new output directory; existing packages are never overwritten")
    files = [ROOT / p for p in ("plugin_server.py", "plugin.json", "mcp.json", ".mcp.json",
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json", "README.md", "LICENSE", ".gitignore")]
    files += sorted((ROOT / "app").glob("*.py"))
    files += [ROOT / "app/settings.example.json"]
    files += [ROOT / "app/static" / name for name in ("index.html", "app.css", "app.js")]
    files += sorted((ROOT / "skills").glob("*/SKILL.md"))
    files += sorted((ROOT / "docs").glob("*.md"))
    files += [ROOT / "examples" / name for name in ("leads.json", "applications.json")]
    for source in files:
        if source.is_symlink() or not source.is_file():
            raise ValueError("Invalid package source: " + str(source.relative_to(ROOT)))
    for source in files:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    print(package(parser.parse_args().out))
