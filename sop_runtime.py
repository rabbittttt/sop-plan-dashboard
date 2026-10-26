"""Locate local runtimes without tying the project to one Windows account."""

import os
import shutil
from pathlib import Path


def node_executable():
    configured = os.environ.get("SOP_NODE_PATH")
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else None

    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node")
