"""Explicit, fail-closed update from the official A2A-Link GitHub checkout."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable, Sequence

OFFICIAL_ORIGIN = "https://github.com/feijhoaaaaa/a2a-link"


class UpdateError(RuntimeError):
    """An update was refused or could not be completed safely."""


def _normalize_remote(value: str) -> str:
    value = value.strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


def _version(root: Path) -> str:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise UpdateError(f"Cannot read project version from {root / 'pyproject.toml'}: {exc}") from exc


def update_installation(
    repository: str | Path | None = None,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    python_executable: str = sys.executable,
) -> dict[str, str | bool]:
    """Fast-forward a clean official checkout and install it into this interpreter."""
    root = Path(repository).resolve() if repository is not None else Path(__file__).resolve().parent.parent

    def execute(args: Sequence[str]) -> subprocess.CompletedProcess:
        try:
            result = run(
                list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )
        except OSError as exc:
            raise UpdateError(f"Could not run {args[0]}: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise UpdateError(f"{args[0]} failed: {detail}")
        return result

    top_level = Path(execute(["git", "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if top_level != root:
        raise UpdateError(f"The installed package is not at the Git checkout root: {root}")

    branch = execute(["git", "branch", "--show-current"]).stdout.strip()
    if branch != "main":
        raise UpdateError("Updates are supported only from the main branch; switch to main and retry.")

    remote = execute(["git", "remote", "get-url", "origin"]).stdout.strip()
    if _normalize_remote(remote) != OFFICIAL_ORIGIN:
        raise UpdateError("The origin is not the official Feijhoaaaaa/A2A-Link GitHub repository.")

    if execute(["git", "status", "--porcelain", "--untracked-files=all"]).stdout.strip():
        raise UpdateError("The checkout must be clean. Commit or save local changes before updating.")

    current_version = _version(root)
    old_sha = execute(["git", "rev-parse", "HEAD"]).stdout.strip()
    execute(["git", "fetch", "origin", "main:refs/remotes/origin/main"])
    execute(["git", "merge", "--ff-only", "origin/main"])
    new_sha = execute(["git", "rev-parse", "HEAD"]).stdout.strip()

    new_version = _version(root)
    try:
        execute([python_executable, "-m", "pip", "install", "-e", "."])
    except UpdateError as exc:
        raise UpdateError(
            f"Source was updated to {new_version}, but installation failed: {exc}. "
            "Retry with the checkout's Python: python -m pip install -e ."
        ) from exc
    changed = new_sha != old_sha
    return {
        "status": "updated" if changed else "up_to_date",
        "from_version": current_version,
        "to_version": new_version,
        "restart_required": changed,
    }
