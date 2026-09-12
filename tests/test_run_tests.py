import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tests
from tests.suites import ACCEPTANCE_MARKER, acceptance


class RunTestsTests(unittest.TestCase):
    def test_partition_preserves_discovery_and_selects_class_and_method_markers(self):
        class ExampleTests(unittest.TestCase):
            def test_fast(self):
                pass

            @acceptance
            def test_slow(self):
                pass

        @acceptance
        class AcceptanceTests(unittest.TestCase):
            def test_one(self):
                pass

            def test_two(self):
                pass

        @acceptance
        def acceptance_function():
            pass

        discovered = unittest.TestSuite(
            (
                unittest.defaultTestLoader.loadTestsFromTestCase(ExampleTests),
                unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceTests),
                unittest.FunctionTestCase(acceptance_function),
            )
        )

        partition = run_tests.partition_suite(discovered)

        all_ids = [test.id() for test in partition.all_tests]
        self.assertEqual(
            [test.id() for test in partition.fast],
            [all_ids[0]],
        )
        self.assertEqual(
            [test.id() for test in partition.acceptance],
            all_ids[1:],
        )
        self.assertEqual(
            [test.id() for test in partition.selected("all")], all_ids
        )
        self.assertEqual(partition.total_count, 5)
        self.assertIs(acceptance(ExampleTests.test_slow), ExampleTests.test_slow)
        self.assertTrue(getattr(ExampleTests.test_slow, ACCEPTANCE_MARKER))

    def test_main_discovers_in_an_owned_tmpdir_and_preserves_ci_environment(self):
        observed: dict[str, object] = {}

        class RecordingLoader:
            def discover(self, *_args, **_kwargs):
                observed["tmpdir"] = tempfile.gettempdir()
                observed["warnings"] = os.environ.get("PYTHONWARNINGS")
                observed["caddy"] = os.environ.get(
                    "LOCAL_WEB_REQUIRE_CADDY_INTEGRATION"
                )
                return unittest.TestSuite()

        original_tempdir = tempfile.tempdir
        try:
            with tempfile.TemporaryDirectory() as caller_tmpdir:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "TMPDIR": caller_tmpdir,
                            "PYTHONWARNINGS": "error::ResourceWarning",
                            "LOCAL_WEB_REQUIRE_CADDY_INTEGRATION": "1",
                        },
                    ),
                    patch.object(
                        run_tests.unittest,
                        "defaultTestLoader",
                        RecordingLoader(),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    tempfile.tempdir = None
                    self.assertEqual(run_tests.main(["--list"]), 0)
                    owned_tmpdir = Path(str(observed["tmpdir"]))
                    self.assertNotEqual(owned_tmpdir, Path(caller_tmpdir))
                    self.assertEqual(os.environ["TMPDIR"], caller_tmpdir)
                    self.assertEqual(
                        os.environ["PYTHONWARNINGS"], "error::ResourceWarning"
                    )
                    self.assertEqual(
                        os.environ["LOCAL_WEB_REQUIRE_CADDY_INTEGRATION"], "1"
                    )
                self.assertFalse(owned_tmpdir.exists())
            self.assertEqual(observed["warnings"], "error::ResourceWarning")
            self.assertEqual(observed["caddy"], "1")
        finally:
            tempfile.tempdir = original_tempdir


if __name__ == "__main__":
    unittest.main()
