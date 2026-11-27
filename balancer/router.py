import json
import os
import sqlite3

from balancer.repository import get_usage, has_capability

TASK_TYPES = ("simple", "feature", "design")
PROFILE_SOURCES = ("user_override", "task_type", "executor_default")


class ProfileError(Exception):
    """A profile could not be resolved; `code` and `detail` go straight into a 422 body."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def load_profile_config(path: str | None = None) -> dict:
    """Read the profile JSON named by A2A_LINK_PROFILE_CONFIG (re-read on every call)."""
    path = path or os.environ.get("A2A_LINK_PROFILE_CONFIG")
    if not path:
        raise ProfileError("profile_config_missing",
                           "A2A_LINK_PROFILE_CONFIG is not set; pass user_override or configure profiles")
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        raise ProfileError("profile_config_invalid", f"cannot read {path}: {e}") from None
    if not isinstance(config, dict) or not isinstance(config.get("agents"), dict):
        raise ProfileError("profile_config_invalid", f"{path} needs an 'agents' object")
    return config


def _validated(agent: str, label: str, profile, agent_cfg: dict) -> dict:
    """A configured profile must name a model (and effort) that the config lists as available."""
    models = agent_cfg.get("models")
    if not isinstance(profile, dict) or not isinstance(models, dict):
        raise ProfileError("profile_unavailable",
                           f"{agent} {label}: profile or models list is missing in the configuration")
    model, effort = profile.get("model"), profile.get("effort")
    if model not in models:
        raise ProfileError("profile_unavailable",
                           f"{agent} {label}: model {model!r} is not available "
                           f"(configured models: {sorted(models)})")
    efforts = models[model]
    if effort is not None and effort not in efforts:
        raise ProfileError("profile_unavailable",
                           f"{agent} {label}: effort {effort!r} is not available for model {model!r} "
                           f"(configured efforts: {efforts})")
    return {"model": model, "effort": effort}


def resolve_profile(agent: str, task_type: str, user_override: dict | None,
                    config_path: str | None = None) -> tuple[dict, str]:
    """user override > task type profile > executor default; never a silent fallback.

    An override is passed through exactly as given (not validated, not merged). A
    configured profile that names an unavailable model or effort is an error, even if a
    lower-precedence profile would have worked.
    """
    if user_override and (user_override.get("model") is not None
                          or user_override.get("effort") is not None):
        return ({"model": user_override.get("model"), "effort": user_override.get("effort")},
                "user_override")
    config = load_profile_config(config_path)
    agent_cfg = config["agents"].get(agent)
    if not isinstance(agent_cfg, dict):
        raise ProfileError("profile_unavailable", f"no profiles are configured for agent {agent!r}")
    task_profile = (agent_cfg.get("task_types") or {}).get(task_type)
    if task_profile is not None:
        return _validated(agent, f"task type {task_type!r}", task_profile, agent_cfg), "task_type"
    default = agent_cfg.get("default")
    if default is None:
        raise ProfileError("profile_unavailable",
                           f"{agent}: no profile for task type {task_type!r} and no executor default")
    return _validated(agent, "executor default", default, agent_cfg), "executor_default"


def choose_agent(conn: sqlite3.Connection, candidates: list[str],
                 required_capability: str | None,
                 usage_ceiling_pct: float = 90.0) -> str | None:
    """Pick the whole-task owner: capable, under the 5h ceiling, lowest usage."""
    best: tuple[float, str] | None = None
    for agent in candidates:
        if required_capability and not has_capability(conn, agent, required_capability):
            continue
        row = get_usage(conn, agent)
        pct = (row["pct_used"] or 0.0) if row else 0.0
        if pct >= usage_ceiling_pct:
            continue
        if best is None or pct < best[0]:
            best = (pct, agent)
    return best[1] if best else None
