"""Allowlist policy for the local terminal executor.

The policy is loaded once at server start from a JSON file that a human owns.
Anything not explicitly allowed is denied (fail closed).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath

IS_WINDOWS = sys.platform == "win32"


class Level(str, Enum):
    SAFE = "safe"        # run without asking
    CONFIRM = "confirm"  # run only after the human approves it (MCP elicitation)
    DENY = "deny"        # never run


# Programs that interpret arbitrary code/strings or change system state. They can
# never be allowlisted: allowing them would amount to an unrestricted `sh -c`.
HARD_DENY = frozenset({
    "cmd", "command", "powershell", "pwsh", "powershell_ise", "bash", "sh", "zsh", "fish",
    "dash", "ksh", "csh", "tcsh", "wsl", "wscript", "cscript", "mshta", "rundll32",
    "regsvr32", "reg", "regedit", "schtasks", "at", "sc", "runas", "sudo", "su", "doas",
    "env", "xargs", "eval", "exec", "nohup", "start", "bitsadmin", "certutil", "msiexec",
    "netsh", "bcdedit", "diskpart", "format", "takeown", "icacls", "cacls",
})

BATCH_EXTENSIONS = (".bat", ".cmd")
DEFAULT_ENV_PASSTHROUGH = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                           "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
                           "LOCALAPPDATA", "LANG", "LC_ALL", "TZ")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    prefix: tuple[str, ...]
    level: Level


@dataclass(frozen=True)
class CommandPolicy:
    name: str
    default: Level = Level.DENY
    rules: tuple[Rule, ...] = ()
    deny_args: tuple[str, ...] = ()
    confirm_args: tuple[str, ...] = ()
    path: str | None = None  # pinned executable; otherwise looked up on PATH

    def level_for(self, args: list[str]) -> tuple[Level, str]:
        best: Rule | None = None
        for rule in self.rules:
            if tuple(args[:len(rule.prefix)]) == rule.prefix:
                if best is None or len(rule.prefix) > len(best.prefix):
                    best = rule
        if best is None:
            return self.default, f"default level for {self.name!r}"
        return best.level, f"rule {self.name} {' '.join(best.prefix)}".rstrip()


@dataclass(frozen=True)
class Policy:
    allowed_roots: tuple[Path, ...]
    commands: dict[str, CommandPolicy]
    audit_log: Path
    default_timeout_s: int = 60
    max_timeout_s: int = 600
    max_output_bytes: int = 200_000
    env_passthrough: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH
    env_set: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    level: Level
    reason: str
    executable: str | None = None
    cwd: str | None = None


def normalize_name(name: str) -> str:
    n = name.lower() if IS_WINDOWS else name
    if IS_WINDOWS and n.endswith(".exe"):
        n = n[:-4]
    return n


def _level(value: str, where: str) -> Level:
    try:
        return Level(value)
    except ValueError:
        raise PolicyError(f"{where}: level must be one of safe/confirm/deny, got {value!r}") from None


def _pinned_path(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    exe = Path(value)
    if not exe.is_absolute() or not exe.is_file():
        raise PolicyError(f"commands.{name}.path must be an absolute path to an existing file: {value}")
    return str(exe)


def load_policy(path: str | os.PathLike) -> Policy:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = tuple(Path(r).resolve(strict=True) for r in data.get("allowed_roots", []))
    if not roots:
        raise PolicyError("allowed_roots must list at least one existing directory")
    for root in roots:
        if not root.is_dir():
            raise PolicyError(f"allowed root is not a directory: {root}")
        if path.is_relative_to(root):
            # The agent may write inside allowed roots; it must not be able to edit its own policy.
            raise PolicyError(f"policy file {path} must live outside allowed root {root}")

    commands: dict[str, CommandPolicy] = {}
    for name, spec in data.get("commands", {}).items():
        key = normalize_name(name)
        if key in HARD_DENY:
            raise PolicyError(f"{name!r} is a shell/system tool and can never be allowlisted")
        rules = tuple(Rule(tuple(r["prefix"]), _level(r["level"], f"commands.{name}.rules"))
                      for r in spec.get("rules", []))
        commands[key] = CommandPolicy(
            name=key,
            default=_level(spec.get("default", "deny"), f"commands.{name}.default"),
            rules=rules,
            deny_args=tuple(spec.get("deny_args", [])),
            confirm_args=tuple(spec.get("confirm_args", [])),
            path=_pinned_path(spec.get("path"), name),
        )

    audit = Path(data.get("audit_log") or path.with_name("a2a_link_terminal_audit.jsonl"))
    try:
        # Fail at start-up, not on the first command: every execution must be auditable.
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8"):
            pass
    except OSError as e:
        raise PolicyError(f"audit_log {audit} is not writable: {e}") from None
    return Policy(
        allowed_roots=roots,
        commands=commands,
        audit_log=audit,
        default_timeout_s=int(data.get("default_timeout_s", 60)),
        max_timeout_s=int(data.get("max_timeout_s", 600)),
        max_output_bytes=int(data.get("max_output_bytes", 200_000)),
        env_passthrough=tuple(data.get("env_passthrough", DEFAULT_ENV_PASSTHROUGH)),
        env_set=dict(data.get("env_set", {"GIT_TERMINAL_PROMPT": "0"})),
    )


def _within_roots(p: Path, roots: tuple[Path, ...]) -> bool:
    return any(p == r or p.is_relative_to(r) for r in roots)


def _looks_like_path(s: str) -> bool:
    if not s:
        return False
    parts = s.replace("\\", "/").split("/")
    return (os.path.isabs(s) or PureWindowsPath(s).drive != "" or s.startswith("~")
            or ".." in parts)


def _path_candidates(arg: str) -> list[str]:
    out = [arg]
    if arg.startswith("-") and "=" in arg:
        out.append(arg.split("=", 1)[1])
    return out


def build_env(policy: Policy) -> dict[str, str]:
    keep = {k.upper() if IS_WINDOWS else k for k in policy.env_passthrough}
    env = {k: v for k, v in os.environ.items() if (k.upper() if IS_WINDOWS else k) in keep}
    env.update(policy.env_set)
    return env


def evaluate(policy: Policy, argv: list[str], cwd: str) -> Decision:
    """Decide whether `argv` may run in `cwd`. Pure apart from filesystem/PATH lookups."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return Decision(Level.DENY, "argv must be a non-empty list of strings")
    if any("\x00" in a for a in argv):
        return Decision(Level.DENY, "NUL byte in argument")
    if len(argv) > 256 or sum(len(a) for a in argv) > 32_000:
        return Decision(Level.DENY, "command line too long")

    name = argv[0]
    if "/" in name or "\\" in name or PureWindowsPath(name).drive:
        return Decision(Level.DENY, "argv[0] must be a bare command name from the policy, not a path")
    key = normalize_name(name)
    if key in HARD_DENY:
        return Decision(Level.DENY, f"{name!r} is a shell/system tool; never allowed")
    cmd = policy.commands.get(key)
    if cmd is None:
        return Decision(Level.DENY, f"{name!r} is not in the allowlist")

    try:
        wd = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return Decision(Level.DENY, f"cwd does not exist: {cwd}")
    if not wd.is_dir():
        return Decision(Level.DENY, f"cwd is not a directory: {cwd}")
    if not _within_roots(wd, policy.allowed_roots):
        return Decision(Level.DENY, f"cwd {wd} is outside allowed roots")

    args = argv[1:]
    for arg in args:
        for denied in cmd.deny_args:
            if arg == denied or (denied.startswith("-") and arg.startswith(denied + "=")):
                return Decision(Level.DENY, f"argument {arg!r} is denied for {key!r}")
        for cand in _path_candidates(arg):
            if _looks_like_path(cand):
                target = (wd / os.path.expanduser(cand)).resolve()
                if not _within_roots(target, policy.allowed_roots):
                    return Decision(Level.DENY, f"path argument {cand!r} escapes allowed roots")

    exe = cmd.path or shutil.which(name, path=build_env(policy).get("PATH"))
    if exe is None:
        return Decision(Level.DENY, f"{name!r} not found on PATH (pin it with commands.{key}.path)")
    if os.path.splitext(exe)[1].lower() in BATCH_EXTENSIONS:
        return Decision(Level.DENY, f"{exe} is a .bat/.cmd script that would run through cmd.exe; "
                                    "allowlist the real executable instead")
    if normalize_name(Path(exe).stem) in HARD_DENY:
        return Decision(Level.DENY, f"{name!r} resolves to shell/system tool {exe}")

    level, reason = cmd.level_for(args)
    if level is Level.SAFE and any(a in cmd.confirm_args or
                                   any(a.startswith(c + "=") for c in cmd.confirm_args if c.startswith("-"))
                                   for a in args):
        level, reason = Level.CONFIRM, f"{reason}; escalated by a confirm_args flag"
    return Decision(level, reason, executable=exe, cwd=str(wd))
