"""Runs the front-end test suites in tests/ui (plain Node.js, no packages) against static/app.js.

Each suite is run as its own process and must: exit 0, print "ALL PASSED: N passed, 0 failed" with the expected N, and print the same
number of "  PASS  " lines and no "  FAIL  " line. A missing summary line fails. The counts are pinned, so a test that is dropped silently
fails here. Without node on PATH every test is skipped. Optional env OPS_APP_JS runs the suites against another copy of app.js (used to
prove that a broken copy fails this file; the repository is never mutated). Tested with node v26.8.1 only.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "tests" / "ui"
NODE = shutil.which("node")
SUITES = [("test_pure.js", 116), ("test_dom.js", 38), ("test_inert.js", 10), ("test_chat.js", 47), ("test_drawer.js", 27)]

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed; the UI suites in tests/ui need Node.js (tested with v26.8.1)")


@pytest.mark.parametrize("suite, expected", SUITES, ids=[suite for suite, _ in SUITES])
def test_the_ui_suite_passes_with_the_expected_number_of_tests(suite, expected):
    app = os.environ.get("OPS_APP_JS")
    try:
        result = subprocess.run([NODE, str(UI_DIR / suite)] + ([app] if app else []), cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{suite} did not finish in 120 s")
    out = result.stdout
    failed = re.findall(r"^ {2}FAIL {2}(.*)$", out, re.M)
    assert not failed, f"{len(failed)} failed test(s) in {suite}: {failed[:10]}"
    summary = re.search(r"^ALL PASSED: (\d+) passed, 0 failed$", out, re.M)
    assert summary, f'{suite} printed no "ALL PASSED: N passed, 0 failed" line (exit {result.returncode}). stderr: {result.stderr[:500]}'
    assert result.returncode == 0, f"{suite} exited {result.returncode}"
    assert int(summary.group(1)) == expected, f"{suite} reports {summary.group(1)} passed tests, expected {expected}"
    assert len(re.findall(r"^ {2}PASS {2}", out, re.M)) == expected, f"{suite}: the number of PASS lines is not {expected}"
