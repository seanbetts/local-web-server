import contextlib
import io
import os
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tests


class RunTestsTests(unittest.TestCase):
    def test_discovery_error_fails_acceptance_execution_and_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_broken_import.py").write_text(
                "raise RuntimeError('broken discovery fixture')\n",
                encoding="utf-8",
            )

            for arguments in (
                ["--suite", "acceptance"],
                ["--suite", "acceptance", "--list"],
            ):
                with self.subTest(arguments=arguments):
                    errors = io.StringIO()
                    with (
                        patch.object(run_tests, "ROOT", root),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(errors),
                    ):
                        result = run_tests.main(arguments)

                    self.assertEqual(result, 1)
                    self.assertIn("test_broken_import", errors.getvalue())

    def test_focused_execution_runs_integration_module_without_running_other_tests(self):
        class FocusedCase(unittest.TestCase):
            def runTest(self):
                observed.append("focused")

        observed = []
        loader = unittest.TestLoader()
        with (
            patch.object(run_tests.unittest, "TestLoader", return_value=loader),
            patch.object(loader, "loadTestsFromNames", return_value=unittest.TestSuite([FocusedCase()])) as load,
            patch.object(loader, "discover", side_effect=AssertionError("unrelated discovery")),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(run_tests.main(["tests.test_deploy"]), 0)
        load.assert_called_once_with(["tests.test_deploy"])
        self.assertEqual(observed, ["focused"])

    def test_focused_stress_target_runs_but_explicit_all_excludes_it(self):
        class StressCase(unittest.TestCase):
            def runTest(self):
                observed.append("stress")

        setattr(StressCase, run_tests.STRESS_MARKER, True)
        for arguments, expected in (
            (["tests.test_scale"], ["stress"]),
            (["--suite", "all", "tests.test_scale"], []),
        ):
            with self.subTest(arguments=arguments):
                observed = []
                loader = unittest.TestLoader()
                with (
                    patch.object(run_tests.unittest, "TestLoader", return_value=loader),
                    patch.object(loader, "loadTestsFromNames", return_value=unittest.TestSuite([StressCase()])),
                    patch.object(loader, "discover", side_effect=AssertionError("unrelated discovery")),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(run_tests.main(arguments), 0)
                self.assertEqual(observed, expected)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires Unix sockets")
    def test_owned_tmpdir_supports_nested_unix_socket_fixtures(self):
        with run_tests._owned_test_tmpdir():
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "repository" / "docs" / "architecture.md"
                path.parent.mkdir(parents=True)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))

    def test_main_discovers_in_an_owned_tmpdir_and_preserves_ci_environment(self):
        observed: dict[str, object] = {}

        class RecordingLoader:
            errors: list[str] = []

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
                        "TestLoader",
                        return_value=RecordingLoader(),
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
