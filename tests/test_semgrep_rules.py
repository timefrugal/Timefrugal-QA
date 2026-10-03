"""
Tests for qa_agent/semgrep_rules/python-security.yml's
`unresolved-base-dir-path-join` taint rule.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_static_analysis.py.

Fixture code is deliberately NOT checked into the repo tree as real `.py`
files with ruleid/ok annotations -- a genuinely vulnerable snippet sitting on
disk would itself be a live CRITICAL-severity finding under this very rule,
and would trip this repo's own self-gating qa.yml on this PR. Instead, every
case's snippet lives here as a Python string constant (invisible to any
scanner walking the real source tree) and is only materialized into real .py
files inside a throwaway temp directory for the lifetime of the semgrep
subprocess call.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_RULE_FILE = Path(__file__).resolve().parents[1] / "qa_agent" / "semgrep_rules" / "python-security.yml"
_SEMGREP = shutil.which("semgrep")
_RULE_ID_SUFFIX = "unresolved-base-dir-path-join"


# ---------------------------------------------------------------------------
# MUST FIRE -- regression coverage (pre-existing, must still work)
# ---------------------------------------------------------------------------

_UPPER_CONST_PATHLIB = '''
from pathlib import Path

CACHE_DIR = Path("/var/cache/app")


def read(name):
    p = CACHE_DIR / name
    return p.read_text()
'''

_UPPER_CONST_OSPATH = '''
import os

CACHE_DIR = "/var/cache/app"


def read(name):
    p = os.path.join(CACHE_DIR, name)
    return open(p)
'''

# ---------------------------------------------------------------------------
# MUST FIRE -- new $BASE shapes (jarvis-infra #407 gap). These fail against
# the original ^[A-Z][A-Z0-9_]*$ regex, which is exactly what makes them real
# regression coverage for the fix rather than coverage for the status quo.
# ---------------------------------------------------------------------------

_LOWER_BARE_ROOT = '''
def read(root, name):
    p = root / name
    return open(p)
'''

_LOWER_SNAKE_DIR = '''
def read(skills_dir, name):
    p = skills_dir / name
    return p.read_text()
'''

_ATTR_DIR = '''
class Writer:
    def __init__(self, dir):
        self.dir = dir

    def write(self, name):
        p = self.dir / name
        p.write_text("x")
'''

_ATTR_SNAKE_ROOT = '''
class Spool:
    def __init__(self, spool_root):
        self.spool_root = spool_root

    def check(self, name):
        p = self.spool_root / name
        return p.exists()
'''

_SUBSCRIPT_KEY_DIR = '''
def read(cfg, name):
    p = cfg["proposals_dir"] / name
    return p.open()
'''

_NESTED_SUBSCRIPT_KEY = '''
def read(config, name):
    p = config["paths"]["spool_root"] / name
    return p.read_bytes()
'''

_OSPATH_JOIN_LOWER = '''
import os


def read(notes_dir, name):
    p = os.path.join(notes_dir, name)
    return open(p)
'''

_MODULE_PRIVATE_CONST = '''
from pathlib import Path

_SPOOL = Path("/var/spool/app")


def read(name):
    p = _SPOOL / name
    return p.read_text()
'''

_DOTTED_CONST = '''
import kmod


def read(name):
    p = kmod.QUARANTINE / name
    return p.read_text()
'''

_CAMEL_CASE_ATTR = '''
class Store:
    def __init__(self, baseDir):
        self.baseDir = baseDir

    def read(self, name):
        p = self.baseDir / name
        return p.read_text()
'''

# ---------------------------------------------------------------------------
# MUST FIRE -- new sinks (jarvis-infra #407 gap). Each uses a lowercase base
# (base_dir) to also pin the $BASE gap at the same time.
# ---------------------------------------------------------------------------

_SINK_MAKEDIRS = '''
import os


def make(base_dir, name):
    os.makedirs(os.path.join(base_dir, name), exist_ok=True)
'''

_SINK_REMOVE = '''
import os


def remove(base_dir, name):
    os.remove(os.path.join(base_dir, name))
'''

_SINK_RENAME_SRC = '''
import os


def rename(base_dir, name, dest):
    os.rename(os.path.join(base_dir, name), dest)
'''

_SINK_RENAME_DST = '''
import os


def rename(base_dir, name, src):
    os.rename(src, os.path.join(base_dir, name))
'''

_SINK_REPLACE_SRC = '''
import os


def replace(base_dir, name, dest):
    os.replace(os.path.join(base_dir, name), dest)
'''

_SINK_REPLACE_DST = '''
import os


def replace(base_dir, name, src):
    os.replace(src, os.path.join(base_dir, name))
'''

_SINK_COPYFILE_SRC = '''
import os
import shutil


def copy(base_dir, name, dest):
    shutil.copyfile(os.path.join(base_dir, name), dest)
'''

_SINK_COPYFILE_DST = '''
import os
import shutil


def copy(base_dir, name, src):
    shutil.copyfile(src, os.path.join(base_dir, name))
'''

_SINK_GLOB = '''
import glob
import os


def find(base_dir, pattern):
    return glob.glob(os.path.join(base_dir, pattern))
'''

# ---------------------------------------------------------------------------
# MUST NOT FIRE -- sanitizers
# ---------------------------------------------------------------------------

_SANITIZED_REALPATH = '''
import os


def read(skills_dir, name):
    p = os.path.realpath(os.path.join(skills_dir, name))
    return open(p)
'''

_SANITIZED_RESOLVE_IS_RELATIVE_TO = '''
from pathlib import Path


def read(skills_dir, name):
    p = (skills_dir / name).resolve()
    if p.is_relative_to(Path(skills_dir).resolve()):
        return p.read_text()
    raise ValueError("path escapes skills_dir")
'''

_SANITIZED_ABSPATH = '''
import os


def read(skills_dir, name):
    p = os.path.abspath(os.path.join(skills_dir, name))
    return open(p)
'''

# ---------------------------------------------------------------------------
# MUST NOT FIRE -- false-positive guards for the widened regex. These pin the
# boundary of the fix: if the regex is ever widened further without care,
# these are the cases that would start failing.
# ---------------------------------------------------------------------------

_BASE_NOT_DIR_SHAPED = '''
class Reader:
    def __init__(self, repo):
        self.repo = repo

    def read(self, name):
        p = self.repo / name
        return open(p)
'''

_LITERAL_SEGMENT = '''
def read(skills_dir):
    p = skills_dir / "fixed.json"
    return p.read_text()
'''

_STRING_LITERAL_BASE = '''
import os


def read(name):
    p = os.path.join("some_dir", name)
    return open(p)
'''

_ARITHMETIC_DIVISION = '''
def average(total_path, count):
    avg = total_path / count
    return avg
'''

MUST_FIRE_CASES = {
    "upper_const_pathlib": _UPPER_CONST_PATHLIB,
    "upper_const_ospath": _UPPER_CONST_OSPATH,
    "lower_bare_root": _LOWER_BARE_ROOT,
    "lower_snake_dir": _LOWER_SNAKE_DIR,
    "attr_dir": _ATTR_DIR,
    "attr_snake_root": _ATTR_SNAKE_ROOT,
    "subscript_key_dir": _SUBSCRIPT_KEY_DIR,
    "nested_subscript_key": _NESTED_SUBSCRIPT_KEY,
    "ospath_join_lower": _OSPATH_JOIN_LOWER,
    "module_private_const": _MODULE_PRIVATE_CONST,
    "dotted_const": _DOTTED_CONST,
    "camel_case_attr": _CAMEL_CASE_ATTR,
    "sink_makedirs": _SINK_MAKEDIRS,
    "sink_remove": _SINK_REMOVE,
    "sink_rename_src": _SINK_RENAME_SRC,
    "sink_rename_dst": _SINK_RENAME_DST,
    "sink_replace_src": _SINK_REPLACE_SRC,
    "sink_replace_dst": _SINK_REPLACE_DST,
    "sink_copyfile_src": _SINK_COPYFILE_SRC,
    "sink_copyfile_dst": _SINK_COPYFILE_DST,
    "sink_glob": _SINK_GLOB,
}

MUST_NOT_FIRE_CASES = {
    "sanitized_realpath": _SANITIZED_REALPATH,
    "sanitized_resolve_is_relative_to": _SANITIZED_RESOLVE_IS_RELATIVE_TO,
    "sanitized_abspath": _SANITIZED_ABSPATH,
    "base_not_dir_shaped": _BASE_NOT_DIR_SHAPED,
    "literal_segment": _LITERAL_SEGMENT,
    "string_literal_base": _STRING_LITERAL_BASE,
    "arithmetic_division": _ARITHMETIC_DIVISION,
}

ALL_CASES = {**MUST_FIRE_CASES, **MUST_NOT_FIRE_CASES}


@unittest.skipUnless(_SEMGREP, "semgrep is not installed")
class TestUnresolvedBaseDirPathJoinRule(unittest.TestCase):
    """
    jarvis-infra #407: `unresolved-base-dir-path-join`'s $BASE metavariable-
    regex only matched ALL-CAPS names, missing most real-world lowercase/
    attribute/subscript path-join bases found during jarvis-infra issue
    #336's audit; its sanitizer list was missing os.path.realpath(...); and
    its sink list was missing several common filesystem-mutation functions
    (os.makedirs/remove/rename/replace, shutil.copyfile, glob.glob). Zero
    live vulnerabilities existed -- this was a pure detection-tooling gap.

    One semgrep invocation (in setUpClass) covers all 28 cases -- regression,
    new-coverage, sanitizers, and false-positive guards -- to keep this fast;
    results are parsed once and asserted on per-case below.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        tmp_path = Path(cls._tmp.name)

        for name, code in ALL_CASES.items():
            (tmp_path / f"{name}.py").write_text(code)

        proc = subprocess.run(
            [
                _SEMGREP, "scan",
                "--config", str(_RULE_FILE),
                "--json", "--quiet", "--metrics=off", "--disable-version-check",
                str(tmp_path),
            ],
            capture_output=True, text=True, timeout=300,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise AssertionError(
                f"semgrep did not produce valid JSON (rc={proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )

        cls.errors = data.get("errors", [])

        hits = {}
        for result in data.get("results", []):
            check_id = result.get("check_id", "")
            if not check_id.endswith(_RULE_ID_SUFFIX):
                continue
            case_name = Path(result["path"]).stem
            hits.setdefault(case_name, []).append(result["start"]["line"])
        cls.hits = hits

    def test_no_semgrep_config_errors(self):
        # Catches a broken rule file silently producing zero findings instead
        # of surfacing a config problem.
        self.assertEqual(self.errors, [], f"semgrep reported config errors: {self.errors}")

    def test_cases_that_must_fire(self):
        for name in MUST_FIRE_CASES:
            with self.subTest(case=name):
                self.assertIn(
                    name, self.hits,
                    f"expected case '{name}' to trigger {_RULE_ID_SUFFIX}, "
                    f"but it produced no findings",
                )

    def test_cases_that_must_not_fire(self):
        for name in MUST_NOT_FIRE_CASES:
            with self.subTest(case=name):
                self.assertNotIn(
                    name, self.hits,
                    f"expected case '{name}' to stay clean of {_RULE_ID_SUFFIX}, "
                    f"but it fired at line(s) {self.hits.get(name)}",
                )

    def test_rule_file_validates(self):
        proc = subprocess.run(
            [_SEMGREP, "scan", "--validate", "--config", str(_RULE_FILE)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"semgrep --validate failed:\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
