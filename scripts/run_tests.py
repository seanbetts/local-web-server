#!/usr/bin/env python3
"""Select and run the repository's fast, acceptance, or complete Python suite."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.suites import ACCEPTANCE_MARKER  # noqa: E402


TestCase = unittest.case.TestCase


@dataclass(frozen=True)
class SuitePartition:
    all_tests: tuple[TestCase, ...]
    fast: tuple[TestCase, ...]
    acceptance: tuple[TestCase, ...]

    @property
    def total_count(self) -> int:
        return len(self.all_tests)

    def selected(self, suite_name: str) -> tuple[TestCase, ...]:
        if suite_name == "fast":
            return self.fast
        if suite_name == "acceptance":
            return self.acceptance
        if suite_name == "all":
            return self.all_tests
        raise ValueError(f"unknown suite: {suite_name}")


def _iter_tests(suite: unittest.TestSuite) -> tuple[TestCase, ...]:
    tests: list[TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_iter_tests(item))
        else:
            tests.append(item)
    return tuple(tests)


def _is_acceptance(test: TestCase) -> bool:
    if getattr(test, ACCEPTANCE_MARKER, False):
        return True
    if getattr(type(test), ACCEPTANCE_MARKER, False):
        return True
    test_function = getattr(test, "_testFunc", None)
    if getattr(test_function, ACCEPTANCE_MARKER, False):
        return True
    method_name = getattr(test, "_testMethodName", None)
    method = getattr(test, method_name, None) if method_name else None
    return bool(getattr(method, ACCEPTANCE_MARKER, False))


def partition_suite(suite: unittest.TestSuite) -> SuitePartition:
    all_tests = _iter_tests(suite)
    fast: list[TestCase] = []
    acceptance_tests: list[TestCase] = []
    for test in all_tests:
        destination = acceptance_tests if _is_acceptance(test) else fast
        destination.append(test)
    return SuitePartition(all_tests, tuple(fast), tuple(acceptance_tests))


def _counts(suite_name: str, partition: SuitePartition) -> tuple[int, int]:
    selected = len(partition.selected(suite_name))
    return selected, partition.total_count - selected


def _report_counts(suite_name: str, partition: SuitePartition) -> None:
    selected, excluded = _counts(suite_name, partition)
    print(
        f"Python suite {suite_name}: selected: {selected}; "
        f"excluded: {excluded}; discovered: {partition.total_count}"
    )


def list_suite(suite_name: str, partition: SuitePartition) -> int:
    _report_counts(suite_name, partition)
    for test in partition.selected(suite_name):
        print(test.id())
    return 0


class TimedTextTestResult(unittest.TextTestResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.timings: list[tuple[float, str]] = []
        self._started_at = 0.0

    def startTest(self, test: TestCase) -> None:
        self._started_at = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: TestCase) -> None:
        self.timings.append((time.perf_counter() - self._started_at, test.id()))
        super().stopTest(test)


def run_suite(suite_name: str, partition: SuitePartition) -> int:
    _report_counts(suite_name, partition)
    suite = unittest.TestSuite(partition.selected(suite_name))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimedTextTestResult)
    result = runner.run(suite)
    assert isinstance(result, TimedTextTestResult)
    print("Slowest tests:")
    for duration, test_id in sorted(result.timings, reverse=True)[:10]:
        print(f"{duration:8.3f}s {test_id}")
    return 0 if result.wasSuccessful() else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("fast", "acceptance", "all"),
        default="fast",
        help="test group to select (default: fast)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list selected test IDs without running them",
    )
    return parser


def _discover_suite() -> unittest.TestSuite | None:
    loader = unittest.TestLoader()
    try:
        discovered = loader.discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT)
        )
    except Exception as error:
        print(f"Test discovery failed: {error}", file=sys.stderr)
        return None
    if loader.errors:
        print("Test discovery failed:", file=sys.stderr)
        for error in loader.errors:
            print(error.rstrip(), file=sys.stderr)
        return None
    return discovered


@contextmanager
def _owned_test_tmpdir():
    previous_environment = os.environ.get("TMPDIR")
    previous_tempdir = tempfile.tempdir
    owner = tempfile.TemporaryDirectory(prefix="local-web-python-tests-")
    private_tmpdir = Path(owner.name) / "tmp"
    private_tmpdir.mkdir(mode=0o700)
    private_tmpdir.chmod(0o700)
    os.environ["TMPDIR"] = str(private_tmpdir)
    tempfile.tempdir = str(private_tmpdir)
    try:
        yield private_tmpdir
    finally:
        if previous_environment is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_environment
        tempfile.tempdir = previous_tempdir
        owner.cleanup()


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    with _owned_test_tmpdir():
        discovered = _discover_suite()
        if discovered is None:
            return 1
        partition = partition_suite(discovered)
        if options.list:
            return list_suite(options.suite, partition)
        return run_suite(options.suite, partition)


if __name__ == "__main__":
    raise SystemExit(main())
