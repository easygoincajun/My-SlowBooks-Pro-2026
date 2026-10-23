"""The browser-side suites written for node's own test runner.

tests/js/*_probe.js print JSON for a pytest test to read. The *.test.cjs
files (#192: the invoice logo option, the journal's void) use node:test
and assert for themselves, and nothing ran them: CI runs pytest, and no
pytest test named them. Each one runs here with `node --test`, and a
failing case, or an error thrown after a case ended, fails the build.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITES = sorted((ROOT / "tests" / "js").glob("*.test.cjs"))


def test_the_node_suites_are_collected():
    names = {suite.name for suite in SUITES}
    assert {
        "invoice_logo_controls.test.cjs",
        "invoice_logo_option.test.cjs",
        "journal_void.test.cjs",
    } <= names


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("suite", SUITES, ids=lambda path: path.name)
def test_node_suite_passes(suite):
    out = subprocess.run(
        ["node", "--test", str(suite)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=ROOT,
    )
    report = out.stdout + out.stderr
    assert out.returncode == 0, report[-6000:]
    # node reports an error thrown after a case finished (a global the page
    # uses and the fake page lacks) as a warning, and still exits 0.
    assert "generated asynchronous activity" not in report, report[-6000:]
