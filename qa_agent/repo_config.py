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


@dataclass
class RepoConfig:
    ai_blocking: bool = False
    block_merge_threshold: Optional[str] = None  # None = use config.py's default
    severity_overrides: Dict = field(default_factory=dict)  # {tool: {category: severity}}
    ignore: Dict = field(default_factory=dict)  # {tool: [rule_id, ...]}


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
    ai = data.get("ai", {}) or {}
    if not isinstance(ai, dict):
        print(
            f"[repo_config] {path}: 'ai' did not parse to a mapping -- ignoring",
            file=sys.stderr,
        )
        ai = {}
    severity_overrides = data.get("severity_overrides", {}) or {}
    if not isinstance(severity_overrides, dict):
        print(
            f"[repo_config] {path}: 'severity_overrides' did not parse to a mapping"
            " -- ignoring",
            file=sys.stderr,
        )
        severity_overrides = {}
    ignore = data.get("ignore", {}) or {}
    if not isinstance(ignore, dict):
        print(
            f"[repo_config] {path}: 'ignore' did not parse to a mapping -- ignoring",
            file=sys.stderr,
        )
        ignore = {}
    return RepoConfig(
        ai_blocking=bool(ai.get("blocking", False)),
        block_merge_threshold=data.get("block_merge_threshold"),
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
