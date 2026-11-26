"""Runs an agent CLI (`claude -p`, `codex exec`) for delegation."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from mcp_server.process import IS_WINDOWS, run_process

# cmd.exe re-parses the command line of .bat/.cmd files; these characters would let
# a prompt break out into extra commands. Used only when a shim cannot be unwrapped.
_CMD_METACHARS = set('"%^&|<>!\r\n')

# npm's generated Windows shim ends with: "%_prog%"  "%dp0%\node_modules\...\cli.js" %*
_NPM_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+\.(?:js|cjs|mjs))"', re.IGNORECASE)

# Where installers put agent CLIs when they are not on the server's PATH.
_EXTRA_LOCATIONS = {
    "claude": [Path.home() / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")],
}


def resolve_executable(name: str) -> str | None:
    override = os.environ.get(f"A2A_LINK_{name.upper()}_BIN")
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which(name)
    if found:
        return found
    for cand in _EXTRA_LOCATIONS.get(name, []):
        if cand.is_file():
            return str(cand)
    return None


def unwrap_npm_shim(path: str) -> list[str] | None:
    """Turn an npm `.cmd` shim into `[node, script.js]` so no cmd.exe parsing happens."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _NPM_SHIM_TARGET.search(text)
    if not m:
        return None
    script = Path(path).parent / m.group(1)
    local_node = Path(path).parent / "node.exe"
    node = str(local_node) if local_node.is_file() else shutil.which("node")
    if node is None or not script.is_file():
        return None
    return [node, str(script)]


def build_argv(command: list[str]) -> tuple[list[str] | None, str]:
    """Resolve command[0] to something runnable without a shell; returns (argv, error)."""
    exe = resolve_executable(command[0])
    if exe is None:
        return None, f"executable not found: {command[0]}"
    if IS_WINDOWS and Path(exe).suffix.lower() in (".bat", ".cmd"):
        unwrapped = unwrap_npm_shim(exe)
        if unwrapped:
            return [*unwrapped, *command[1:]], ""
        if any(ch in _CMD_METACHARS for arg in command[1:] for ch in arg):
            return None, (f"{exe} is a batch script run through cmd.exe; refusing arguments "
                          "containing cmd metacharacters (\"%^&|<>!) to prevent injection")
    return [exe, *command[1:]], ""


def run_cli_task(command: list[str], cwd: str, timeout_s: int = 300,
                 env: dict[str, str] | None = None, **progress) -> tuple[int, str, str]:
    """Returns (returncode, stdout, stderr); never raises for timeout / missing binary.

    `progress` is passed to run_process: on_line, heartbeat_s, on_heartbeat.
    """
    argv, error = build_argv(command)
    if argv is None:
        return (127 if error.startswith("executable not found") else 126), "", error
    try:
        r = run_process(argv, cwd=cwd, timeout_s=timeout_s, env=env, **progress)
    except OSError as e:
        return 126, "", f"failed to start {argv[0]}: {e}"
    return r.returncode, r.stdout, r.stderr
