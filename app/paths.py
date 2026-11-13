"""Version-independent private storage shared by local agent clients."""
import os
from pathlib import Path


def data_dir():
    override = os.environ.get("LEAD_WORKBENCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "lead-workbench"
