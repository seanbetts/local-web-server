#!/usr/bin/env python3
"""Run focused Python checks, integrations, or the optional supported-scale test."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.suites import ACCEPTANCE_MARKER, STRESS_MARKER  # noqa: E402


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def marked(test, marker):
    return any(getattr(item, marker, False) for item in (
        test, type(test), getattr(test, test._testMethodName, None),
    ))


@contextmanager
def _owned_test_tmpdir():
    previous_environment = os.environ.get("TMPDIR")
    previous_tempdir = tempfile.tempdir
    # Short private paths keep nested Unix sockets within macOS's 104-byte limit.
    with tempfile.TemporaryDirectory(prefix="lw-tests-", dir="/tmp") as directory:
        os.environ["TMPDIR"] = directory
        tempfile.tempdir = directory
        try:
            yield Path(directory)
        finally:
            if previous_environment is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = previous_environment
            tempfile.tempdir = previous_tempdir


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("fast", "acceptance", "all", "stress"), default="fast")
    parser.add_argument("--list", action="store_true")
    options = parser.parse_args(arguments)
    with _owned_test_tmpdir():
        loader = unittest.TestLoader()
        try:
            discovered = list(iter_tests(loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))))
        except Exception as error:
            print(f"Test discovery failed: {error}", file=sys.stderr)
            return 1
        if loader.errors:
            print("\n".join(loader.errors), file=sys.stderr)
            return 1
        selected = []
        for test in discovered:
            group = "stress" if marked(test, STRESS_MARKER) else (
                "acceptance" if marked(test, ACCEPTANCE_MARKER) else "fast")
            if group == options.suite or (options.suite == "all" and group != "stress"):
                selected.append(test)
        print(f"Python {options.suite}: {len(selected)} selected, {len(discovered) - len(selected)} excluded", flush=True)
        if options.list:
            for test in selected:
                print(test.id())
            return 0
        result = unittest.TextTestRunner(verbosity=1, durations=10).run(unittest.TestSuite(selected))
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
