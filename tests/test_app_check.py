import json
import os
import tempfile
import unittest
from pathlib import Path

from local_web_server.app_check import AppCheckError, AppChecker
from local_web_server.app_doctor import Diagnostic, DoctorReport
from local_web_server.process_runner import ProcessCommand, ProcessRunError


class FixedDoctor:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def inspect(self, repository):
        self.calls.append(repository)
        return self.report


class RecordingRunner:
    def __init__(self, failure=None, events=None):
        self.calls = []
        self.failure = failure
        self.events = events

    def run(self, repository, commands):
        self.calls.append((repository, commands))
        if self.events is not None:
            self.events.append("app-local-checks")
        if self.failure is not None:
            raise self.failure


class RecordingComposer:
    def __init__(self, release=None, failure=None, events=None):
        self.calls = []
        self.release = release
        self.failure = failure
        self.events = events

    def compose(self, repository, build):
        self.calls.append((repository, build))
        if self.events is not None:
            self.events.append("release-composition")
        if self.failure is not None:
            raise self.failure
        if self.release is not None:
            return self.release
        return repository / build.output


class AppCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "recipes"
        self.repository.mkdir()
        self._write_manifest("static")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_manifest(self, kind, *, frontend_output=True):
        payload = {
            "schemaVersion": 1,
            "id": "recipes",
            "title": "Recipes",
            "route": "/recipes",
            "kind": kind,
            "build": {
                "commands": [["npm", "run", "build"]],
                "output": "release" if kind == "service" else "dist",
                "environment": [],
            },
            "healthPath": "/recipes/healthz" if kind == "service" else "/recipes/",
        }
        if kind == "service":
            payload["build"]["release"] = [
                {"source": "dist", "target": "public"},
            ]
            payload["service"] = {
                "module": "recipes.api",
                "internalHealthPath": "/healthz",
                "startCommand": [
                    "/usr/bin/env",
                    "node",
                    "{release}/server/service.mjs",
                    "--port",
                    "{port}",
                    "--data-dir",
                    "{repository}/data",
                ],
            }
            if frontend_output:
                payload["service"]["frontendOutput"] = "public"
                payload["service"]["proxyPaths"] = ["/api"]
        (self.repository / "local-web.json").write_text(json.dumps(payload))

    def write_release_index(self, relative, content):
        entry = self.repository / relative
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(content, encoding="utf-8")
        return entry

    def assert_release_refused(self, composer, secret):
        with self.assertRaisesRegex(
            AppCheckError, "^app release contract failed$"
        ) as raised:
            AppChecker(
                doctor=FixedDoctor(DoctorReport("recipes", True, ())),
                process_runner=RecordingRunner(),
                release_composer=composer,
            ).check(self.repository)

        self.assertNotIn(str(self.repository), str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_runs_structural_doctor_then_the_two_app_local_commands(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )
        report = DoctorReport("recipes", True, ())
        doctor = FixedDoctor(report)
        runner = RecordingRunner()
        composer = RecordingComposer()

        result = AppChecker(
            doctor=doctor, process_runner=runner, release_composer=composer
        ).check(self.repository)

        self.assertIs(result, report)
        self.assertEqual(doctor.calls, [self.repository])
        self.assertEqual(
            runner.calls,
            [
                (
                    self.repository,
                    (
                        ProcessCommand("npm-check", ("npm", "run", "check")),
                        ProcessCommand("npm-e2e", ("npm", "run", "test:e2e")),
                    ),
                )
            ],
        )
        self.assertEqual(len(composer.calls), 1)
        self.assertEqual(composer.calls[0][0], self.repository)
        self.assertEqual(composer.calls[0][1].output, Path("dist"))

    def test_service_app_runs_same_commands_then_composes_declared_release(self):
        self._write_manifest("service")
        self.write_release_index(
            Path("release/public/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )
        events = []
        runner = RecordingRunner(events=events)
        composer = RecordingComposer(events=events)

        AppChecker(
            doctor=FixedDoctor(DoctorReport("recipes", True, ())),
            process_runner=runner,
            release_composer=composer,
        ).check(self.repository)

        self.assertEqual(
            runner.calls,
            [
                (
                    self.repository,
                    (
                        ProcessCommand("npm-check", ("npm", "run", "check")),
                        ProcessCommand("npm-e2e", ("npm", "run", "test:e2e")),
                    ),
                )
            ],
        )
        self.assertEqual(
            composer.calls[0][1].release_entries[0].target, Path("public")
        )
        self.assertEqual(events, ["app-local-checks", "release-composition"])

    def test_static_check_requires_a_real_built_entry_with_one_theme_link(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )

        AppChecker(
            doctor=FixedDoctor(DoctorReport("recipes", True, ())),
            process_runner=RecordingRunner(),
            release_composer=RecordingComposer(self.repository / "dist"),
        ).check(self.repository)

    def test_service_check_validates_frontend_output_beneath_release(self):
        self._write_manifest("service")
        self.write_release_index(
            Path("release/public/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )

        AppChecker(
            doctor=FixedDoctor(DoctorReport("recipes", True, ())),
            process_runner=RecordingRunner(),
            release_composer=RecordingComposer(self.repository / "release"),
        ).check(self.repository)

    def test_complete_output_service_keeps_its_entry_at_the_release_root(self):
        self._write_manifest("service", frontend_output=False)
        self.write_release_index(
            Path("release/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )

        AppChecker(
            doctor=FixedDoctor(DoctorReport("recipes", True, ())),
            process_runner=RecordingRunner(),
            release_composer=RecordingComposer(self.repository / "release"),
        ).check(self.repository)

    def test_release_refuses_an_absent_output(self):
        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "missing")

    def test_release_refuses_an_output_file(self):
        (self.repository / "dist").write_text("private output", encoding="utf-8")

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "private")

    def test_release_refuses_an_output_outside_the_repository(self):
        outside = Path(self.temporary.name) / "private-output"
        outside.mkdir()
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )

        self.assert_release_refused(RecordingComposer(outside), "private-output")

    def test_release_refuses_a_symlinked_output(self):
        target = self.repository / "private-output"
        target.mkdir()
        (target / "index.html").write_text(
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
            encoding="utf-8",
        )
        (self.repository / "dist").symlink_to(target, target_is_directory=True)

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "private-output")

    def test_release_refuses_a_composer_returned_alias_to_declared_output(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )
        alias = self.repository / "release-alias"
        alias.symlink_to(self.repository / "dist", target_is_directory=True)

        self.assert_release_refused(RecordingComposer(alias), "release-alias")

    def test_release_refuses_a_symlinked_service_frontend(self):
        self._write_manifest("service")
        (self.repository / "release").mkdir()
        target = self.repository / "private-frontend"
        target.mkdir()
        (target / "index.html").write_text(
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
            encoding="utf-8",
        )
        (self.repository / "release/public").symlink_to(
            target, target_is_directory=True
        )

        self.assert_release_refused(RecordingComposer(self.repository / "release"), "private-frontend")

    def test_release_refuses_a_symlinked_entry(self):
        (self.repository / "dist").mkdir()
        target = self.repository / "private-index.html"
        target.write_text(
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
            encoding="utf-8",
        )
        (self.repository / "dist/index.html").symlink_to(target)

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "private-index")

    def test_release_refuses_a_special_file_entry(self):
        (self.repository / "dist").mkdir()
        os.mkfifo(self.repository / "dist/index.html")

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "index.html")

    def test_release_refuses_an_absent_entry(self):
        (self.repository / "dist").mkdir()

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "index.html")

    def test_release_refuses_duplicate_theme_links(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">'
            '<link rel="stylesheet" href="/_local-web/platform/theme.css">',
        )

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "theme.css")

    def test_release_refuses_duplicate_case_insensitive_href_attributes(self):
        cases = (
            (
                "first-valid",
                '<link rel="stylesheet" HREF="/_local-web/platform/theme.css" '
                'href="/not-theme.css">',
            ),
            (
                "last-valid",
                '<link rel="stylesheet" href="/not-theme.css" '
                'HREF="/_local-web/platform/theme.css">',
            ),
        )

        for order, content in cases:
            with self.subTest(order=order):
                self.write_release_index(Path("dist/index.html"), content)
                self.assert_release_refused(
                    RecordingComposer(self.repository / "dist"), "not-theme"
                )

    def test_release_refuses_duplicate_case_insensitive_rel_attributes(self):
        cases = (
            (
                "first-valid",
                '<link REL="stylesheet" rel="alternate" '
                'href="/_local-web/platform/theme.css">',
            ),
            (
                "last-valid",
                '<link rel="alternate" REL="stylesheet" '
                'href="/_local-web/platform/theme.css">',
            ),
        )

        for order, content in cases:
            with self.subTest(order=order):
                self.write_release_index(Path("dist/index.html"), content)
                self.assert_release_refused(
                    RecordingComposer(self.repository / "dist"), "alternate"
                )

    def test_release_refuses_a_theme_looking_string_without_a_stylesheet_link(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<script>"/_local-web/platform/theme.css"</script>',
        )

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "theme.css")

    def test_release_refuses_the_wrong_theme_route(self):
        self.write_release_index(
            Path("dist/index.html"),
            '<link rel="stylesheet" href="/_local-web/platform/not-theme.css">',
        )

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "not-theme")

    def test_release_refuses_an_oversized_entry(self):
        entry = self.repository / "dist/index.html"
        entry.parent.mkdir()
        entry.write_bytes(
            b'<link rel="stylesheet" href="/_local-web/platform/theme.css">'
            + b"x" * (2 * 1024 * 1024)
        )

        self.assert_release_refused(RecordingComposer(self.repository / "dist"), "theme.css")

    def test_structural_diagnostic_starts_no_app_process(self):
        report = DoctorReport(
            "recipes",
            False,
            (Diagnostic("manifest.invalid", "error", "Invalid.", "Repair."),),
        )
        runner = RecordingRunner()

        with self.assertRaisesRegex(AppCheckError, "^app has compatibility diagnostics$"):
            AppChecker(doctor=FixedDoctor(report), process_runner=runner).check(
                self.repository
            )

        self.assertEqual(runner.calls, [])

    def test_sanitises_process_runner_failures(self):
        private_failure = ProcessRunError("private child detail")
        runner = RecordingRunner(private_failure)

        with self.assertRaisesRegex(AppCheckError, "^app checks failed$") as raised:
            AppChecker(
                doctor=FixedDoctor(DoctorReport("recipes", True, ())),
                process_runner=runner,
            ).check(self.repository)

        self.assertNotIn("private child detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
