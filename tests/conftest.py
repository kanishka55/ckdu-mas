"""Shared pytest setup.

Embedded-SPADE integration tests (``Embedded*`` test classes, opt-in via ``CKDU_RUN_SPADE_*``) each call
``spade.run()``. That closes the event loop of SPADE's process-wide singleton ``Container`` and leaves
its agents and embedded server state behind, so a second such test in the same pytest process fails
("Event loop is closed") or hangs. Each of these tests therefore runs in its own fresh Python process.
"""
import os
import subprocess
import sys

import pytest

_CHILD_FLAG = "CKDU_SPADE_TEST_CHILD"
_CHILD_TIMEOUT_S = 300


def _is_embedded_spade_test(item) -> bool:
    cls = getattr(item, "cls", None)
    return cls is not None and cls.__name__.startswith("Embedded")


def _run_in_child(item):
    def runtest():
        env = {**os.environ, _CHILD_FLAG: "1"}
        try:
            done = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", item.nodeid],
                cwd=str(item.config.rootpath), env=env, capture_output=True, text=True,
                timeout=_CHILD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(f"{item.nodeid} did not finish within {_CHILD_TIMEOUT_S}s in its own process")
        output = done.stdout + done.stderr
        if " skipped" in output and " passed" not in output:
            pytest.skip("skipped in child process (opt-in environment variable not set)")
        if done.returncode != 0:
            pytest.fail(f"{item.nodeid} failed in its own process:\n{output[-4000:]}", pytrace=False)
    return runtest


def pytest_collection_modifyitems(config, items):
    if os.environ.get(_CHILD_FLAG):
        return  # already the isolated child: run the test normally
    for item in items:
        if _is_embedded_spade_test(item):
            item.runtest = _run_in_child(item)
