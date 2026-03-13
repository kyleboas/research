from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_POLICY = {
    "max_research_rounds": 2,
    "simple_default_rounds": 2,
    "moderate_default_rounds": 3,
    "complex_default_rounds": 5,
    "moderate_min_tasks": 3,
    "complex_min_tasks": 5,
    "subagent_search_limit": 24,
    "subagent_max_tokens": 7000,
    "synthesis_max_tokens": 16000,
    "revision_max_tokens": 16000,
}

_POLICY_ENV_VAR = "REPORT_POLICY_PATH"
_DEFAULT_POLICY_PATH = Path(__file__).with_name("report_policy_config.json")


def get_policy_path() -> Path:
    raw = (os.environ.get(_POLICY_ENV_VAR) or "").strip()
    return Path(raw) if raw else _DEFAULT_POLICY_PATH


def load_policy(overrides: dict | None = None) -> dict:
    policy = dict(DEFAULT_POLICY)
    policy_path = get_policy_path()

    try:
        loaded = json.loads(policy_path.read_text())
    except FileNotFoundError:
        loaded = {}
    except json.JSONDecodeError:
        loaded = {}

    for key, default_value in DEFAULT_POLICY.items():
        if key not in loaded:
            continue
        try:
            policy[key] = type(default_value)(loaded[key])
        except (TypeError, ValueError):
            continue

    if overrides:
        for key, value in overrides.items():
            if key in DEFAULT_POLICY and value is not None:
                policy[key] = type(DEFAULT_POLICY[key])(value)

    return policy


def save_policy(policy: dict, path: str | Path | None = None) -> Path:
    target = Path(path) if path else get_policy_path()
    merged = load_policy(policy)
    target.write_text(json.dumps(merged, indent=2) + "\n")
    return target
