"""Locate local runtimes without tying the project to one Windows account."""

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path


_ARTIFACT_LINK_LOCK = threading.Lock()


def node_executable():
    configured = os.environ.get("SOP_NODE_PATH")
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else None

    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node")


class ArtifactDependencyError(RuntimeError):
    """The local Excel writer cannot resolve its required Node package."""


def _artifact_modules(node):
    configured = os.environ.get("SOP_NODE_MODULES_PATH")
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        executable = Path(node) if node else None
        candidates = ([executable.parent / "node_modules", executable.parent.parent / "node_modules"]
                      if executable else [])
        candidates.append(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
    for candidate in candidates:
        if (candidate / "@oai" / "artifact-tool" / "package.json").is_file():
            return candidate.resolve()
    raise ArtifactDependencyError(
        "找不到 Excel 生成依赖 @oai/artifact-tool。请检查 Codex 运行环境，"
        "或将 SOP_NODE_MODULES_PATH 设为包含 @oai/artifact-tool 的 node_modules 目录。")


def _removable_node_link(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    tag = getattr(details, "st_reparse_tag", None)
    junction_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return path.is_symlink() or (os.name == "nt" and tag is not None and tag == junction_tag)


def ensure_artifact_tool(root, node=None):
    """Make the bundled Node package visible to ESM imports in scripts/.

    Git excludes this local junction. It may be missing after a fresh clone or
    runtime update, while the project source and saved business data are intact.
    """
    modules = Path(root) / "scripts" / "node_modules"
    package = modules / "@oai" / "artifact-tool" / "package.json"
    if package.is_file():
        return
    with _ARTIFACT_LINK_LOCK:
        if package.is_file():
            return
        source = _artifact_modules(node or node_executable())
        if os.path.lexists(modules):
            if not _removable_node_link(modules):
                raise ArtifactDependencyError(
                    "scripts/node_modules 已存在但缺少 @oai/artifact-tool；"
                    "请检查该目录或重新安装 Excel 生成依赖。")
            if modules.is_symlink():
                modules.unlink()
            else:
                os.rmdir(modules)  # Removes only the junction, never its target.
        if os.name == "nt":
            # Pass paths as environment values. PowerShell reads them after parsing
            # the fixed command, so folder names containing &, %, or spaces stay literal.
            environment = {**os.environ, "SOP_RUNTIME_LINK": str(modules),
                           "SOP_RUNTIME_SOURCE": str(source)}
            command = "New-Item -ItemType Junction -Path $env:SOP_RUNTIME_LINK -Target $env:SOP_RUNTIME_SOURCE | Out-Null"
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                       capture_output=True, text=True, errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW, env=environment)
            if completed.returncode and not package.is_file():
                details = (completed.stderr or completed.stdout or "").strip()
                raise ArtifactDependencyError(
                    "无法连接 Excel 生成依赖，请检查 scripts 目录权限。" + (f" {details[-300:]}" if details else ""))
        else:
            try:
                modules.symlink_to(source, target_is_directory=True)
            except FileExistsError:
                pass
        if not package.is_file():
            raise ArtifactDependencyError("Excel 生成依赖链接未生效，请检查 scripts/node_modules。")
