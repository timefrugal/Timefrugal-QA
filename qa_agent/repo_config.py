"""
Per-repo QA configuration — `.timefrugal-qa.yml` at the target project root.

Lets a consuming repo opt into stricter behavior (AI findings blocking merges,
custom severity mappings, per-tool/per-rule waivers) without changing any
global default. An absent, empty, or malformed config file is always
equivalent to today's behavior -- this module never raises.
"""
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from qa_agent import config

# The only severities a per-repo .timefrugal-qa.yml is allowed to name
# (`block_merge_threshold`, `severity_overrides.<tool>.<key>`). Sourced from
# config.SEVERITY_ORDER so this file never carries its own copy of the list
# to drift out of sync with static_analysis.py's threshold ordering.
_VALID_SEVERITIES = set(config.SEVERITY_ORDER)


@dataclass
class RepoConfig:
    ai_blocking: bool = False
    block_merge_threshold: Optional[str] = None  # None = use config.py's default
    severity_overrides: Dict = field(default_factory=dict)  # {tool: {category: severity}}
    ignore: Dict = field(default_factory=dict)  # {tool: [rule_id, ...]}


def _as_mapping(value, field_name: str, path: str) -> dict:
    """Return value if it's a dict, else {} with a stderr warning. Used for
    every YAML field that's expected to parse to a mapping."""
    if not isinstance(value, dict):
        print(
            f"[repo_config] {path}: '{field_name}' did not parse to a mapping"
            " -- ignoring",
            file=sys.stderr,
        )
        return {}
    return value


def _as_severity(value, field_name: str, path: str) -> Optional[str]:
    """Return value if it's one of the known severity strings (exact case),
    else None with a stderr warning. None always means 'fall back to the
    existing default for this field' at the call site."""
    if value is None:
        return None
    if isinstance(value, str) and value in _VALID_SEVERITIES:
        return value
    print(
        f"[repo_config] {path}: '{field_name}' is not a valid severity "
        f"(expected one of {sorted(_VALID_SEVERITIES)}, got {value!r}) -- ignoring",
        file=sys.stderr,
    )
    return None


def _sanitize_severity_overrides(severity_overrides: dict, path: str) -> dict:
    """Validate the nested {tool: {key: severity}} structure leaf-by-leaf.
    A non-mapping per-tool value drops that tool's overrides entirely; an
    individual leaf value that isn't a known severity drops just that one
    entry, keeping any other valid entries for the same tool."""
    sanitized: Dict = {}
    for tool, mapping in severity_overrides.items():
        clean_mapping = _as_mapping(mapping, f"severity_overrides.{tool}", path)
        clean_entries = {}
        for key, val in clean_mapping.items():
            validated = _as_severity(val, f"severity_overrides.{tool}.{key}", path)
            if validated is not None:
                clean_entries[key] = validated
        sanitized[tool] = clean_entries
    return sanitized


def load_repo_config(project_root: str) -> RepoConfig:
    """Load .timefrugal-qa.yml from project_root. Missing file, empty file, or
    parse error all return defaults -- never raises, so an absent config is
    byte-for-byte today's behavior."""
    path = os.path.join(project_root, ".timefrugal-qa.yml")
    if not os.path.isfile(path):
        return RepoConfig()
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[repo_config] could not parse {path}: {e} -- using defaults", file=sys.stderr)
        return RepoConfig()
    if not isinstance(data, dict):
        print(
            f"[repo_config] {path} did not parse to a mapping -- using defaults",
            file=sys.stderr,
        )
        return RepoConfig()
    ai = _as_mapping(data.get("ai", {}) or {}, "ai", path)
    severity_overrides = _as_mapping(
        data.get("severity_overrides", {}) or {}, "severity_overrides", path
    )
    severity_overrides = _sanitize_severity_overrides(severity_overrides, path)
    ignore = _as_mapping(data.get("ignore", {}) or {}, "ignore", path)
    block_merge_threshold = _as_severity(
        data.get("block_merge_threshold"), "block_merge_threshold", path
    )
    return RepoConfig(
        ai_blocking=bool(ai.get("blocking", False)),
        block_merge_threshold=block_merge_threshold,
        severity_overrides=severity_overrides,
        ignore=ignore,
    )


def filter_ignored(findings: List, ignore_map: Dict) -> List:
    """Remove findings whose (tool, rule_id) appears in ignore_map. tool names
    are normalized (hyphens -> underscores) to match YAML keys like pip_audit."""
    if not ignore_map or not isinstance(ignore_map, dict):
        return findings
    out = []
    warned_tool_keys = set()
    for f in findings:
        tool_key = f.tool.replace("-", "_")
        ignored_ids = ignore_map.get(tool_key, [])
        if not isinstance(ignored_ids, list):
            if tool_key not in warned_tool_keys:
                print(
                    f"[repo_config] ignore.{tool_key} did not parse to a list"
                    " -- ignoring",
                    file=sys.stderr,
                )
                warned_tool_keys.add(tool_key)
            ignored_ids = []
        if f.rule_id in ignored_ids:
            continue
        out.append(f)
    return out
