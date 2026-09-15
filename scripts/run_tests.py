#!/usr/bin/env python3
"""Run focused tests or the verification tiers documented in CONTRIBUTING.md."""

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

# Cheap component contracts run on every change. New/unknown modules default to
# integration, so adding tests cannot silently leave them outside verification.
ROUTINE_MODULES = frozenset("""
agent_skill app_check app_doctor app_foundation app_identity app_initialization
app_provenance app_template app_update_transaction cli colour config deploy
git_build git_runner host_profile host_registry icons index_registry ingress
process_runner public_release release_composer render run_tests runtime
service_command service_ports services status system_index_bundle theme theme_gate
""".split())

# Only the real acceptance journeys in these modules are release-level work.
# Their focused tests still run in integration.
RELEASE_MODULES = frozenset("""
app_generator app_update ui_package fleet_update_workflow
foundation_adoption_workflow public_base_path_migration_workflow
service_command_migration_workflow
""".split())


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


def tier(test):
    module = type(test).__module__.removeprefix("tests.test_")
    if marked(test, STRESS_MARKER):
        return "stress"
    if marked(test, ACCEPTANCE_MARKER):
        return "release" if module in RELEASE_MODULES else "integration"
    return "routine" if module in ROUTINE_MODULES else "integration"


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
    parser.add_argument("--suite", choices=("routine", "integration", "release", "all", "stress", "fast", "acceptance"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("tests", nargs="*", help="unittest module, class or method names")
    options = parser.parse_args(arguments)
    options.suite = options.suite or ("all" if options.tests else "routine")
    with _owned_test_tmpdir():
        loader = unittest.TestLoader()
        try:
            suite = loader.loadTestsFromNames(options.tests) if options.tests else loader.discover(
                str(ROOT / "tests"), top_level_dir=str(ROOT))
            discovered = list(iter_tests(suite))
        except Exception as error:
            print(f"Test discovery failed: {error}", file=sys.stderr)
            return 1
        if loader.errors:
            print("\n".join(loader.errors), file=sys.stderr)
            return 1
        selected = []
        for test in discovered:
            group = tier(test)
            include = group == options.suite or (options.suite == "all" and group != "stress")
            # Retain the old direct-runner partition names for existing callers.
            if options.suite in ("fast", "acceptance"):
                include = group != "stress" and marked(test, ACCEPTANCE_MARKER) == (options.suite == "acceptance")
            if include:
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
