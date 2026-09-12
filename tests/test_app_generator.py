import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_doctor import AppDoctor, DoctorReport
from local_web_server.app_generator import (
    AppGenerationError,
    AppGenerator,
    CreateAppRequest,
)
from local_web_server.app_provenance import CURRENT_TEMPLATE_VERSION, load_provenance
from local_web_server.process_runner import ProcessRunError, ProcessRunner
from local_web_server.ui_package import UiPackageArtifact, UiPackageError
from tests.suites import acceptance


ROOT = Path(__file__).parents[1]
UI_BYTES = b"deterministic-ui"
UI_SHA256 = hashlib.sha256(UI_BYTES).hexdigest()


class RecordingDoctor:
    def __init__(self, trace, *, diagnostic=False):
        self.trace = trace
        self.diagnostic = diagnostic

    def inspect(self, repository):
        self.trace.append(("doctor", repository))
        diagnostics = (object(),) if self.diagnostic else ()
        return DoctorReport("recipes", not diagnostics, diagnostics)


class RecordingProcessRunner:
    def __init__(self, trace, *, fail_label=None):
        self.trace = trace
        self.fail_label = fail_label

    def run(self, repository, commands):
        labels = tuple(command.label for command in commands)
        self.trace.append(("process", repository, labels))
        if self.fail_label in labels:
            raise ProcessRunError(f"{self.fail_label} failed")
        for command in commands:
            if command.label == "npm-lock":
                (repository / "package-lock.json").write_text(
                    '{"name":"recipes","lockfileVersion":3}\n', encoding="utf-8"
                )
            if command.label == "npm-install":
                (repository / "node_modules").mkdir()
            if command.label.startswith("git-"):
                completed = subprocess.run(
                    command.argv,
                    cwd=repository,
                    check=False,
                    capture_output=True,
                    env={"PATH": "/usr/bin:/bin"},
                )
                if completed.returncode:
                    raise ProcessRunError(f"{command.label} failed")


class RecordingReleaseComposer:
    def __init__(self):
        self.calls = []

    def compose(self, repository, build):
        self.calls.append((repository, build))
        return repository / build.output


def build_ui(_repository, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(UI_BYTES)
    return UiPackageArtifact("0.1.0", UI_SHA256, output)


def request(destination, **changes):
    values = {
        "app_id": "recipes",
        "title": "Recipe Collection",
        "destination": destination,
        "route": "/recipes",
        "icon": "book",
        "accent": "#8EA7C6",
        "capabilities": (),
        "kind": "static",
    }
    values.update(changes)
    return CreateAppRequest(**values)


class AppGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.workspace = Path(self.temporary.name)
        self.trace = []
        self.generator = AppGenerator(
            ROOT,
            artifact_builder=build_ui,
            doctor=RecordingDoctor(self.trace),
            process_runner=RecordingProcessRunner(self.trace),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_reserved_generic_icon_cannot_generate_a_new_app(self):
        destination = self.workspace / "generic-app"

        with self.assertRaisesRegex(AppGenerationError, "request is invalid"):
            self.generator.preview(request(destination, icon="apps"))

        self.assertFalse(destination.exists())

    def test_publishes_a_complete_main_repository_after_structural_and_app_checks(self):
        """A missing doctor or verification step would publish an unchecked app."""
        destination = self.workspace / "recipes"

        result = self.generator.create(request(destination))

        self.assertEqual(result.destination, destination)
        self.assertEqual(result.commit, self.git_head(destination))
        artifact = destination / "vendor/local-web-ui.tgz"
        self.assertEqual(artifact.read_bytes(), UI_BYTES)
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), UI_SHA256)
        provenance = load_provenance(destination / ".local-web-platform.json")
        self.assertEqual(CURRENT_TEMPLATE_VERSION, 3)
        self.assertEqual(provenance.template_version, CURRENT_TEMPLATE_VERSION)
        self.assertEqual(provenance.ui.version, "0.1.0")
        self.assertEqual(provenance.ui.sha256, UI_SHA256)
        self.assertEqual(result.ui_version, provenance.ui.version)
        self.assertEqual(result.ui_sha256, provenance.ui.sha256)
        package = json.loads((destination / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["dependencies"],
            {
                "@local-web/ui": "file:vendor/local-web-ui.tgz",
                "react": "19.2.8",
                "react-dom": "19.2.8",
            },
        )
        self.assertEqual(
            package["devDependencies"],
            {
                "@eslint/js": "10.0.1",
                "@axe-core/playwright": "4.12.1",
                "@playwright/test": "1.62.1",
                "@testing-library/jest-dom": "7.0.0",
                "@testing-library/react": "16.3.2",
                "@types/node": "26.1.2",
                "@types/react": "19.2.18",
                "@types/react-dom": "19.2.4",
                "@vitejs/plugin-react": "6.0.5",
                "eslint": "10.8.0",
                "eslint-plugin-react-hooks": "7.1.1",
                "eslint-plugin-react-refresh": "0.5.3",
                "globals": "17.9.0",
                "jsdom": "30.0.1",
                "typescript": "6.0.3",
                "typescript-eslint": "8.66.0",
                "vite": "8.2.0",
                "vitest": "4.1.10",
            },
        )
        self.assertTrue((destination / "package-lock.json").is_file())
        self.assertTrue((destination / "src/contextExport.ts").is_file())
        self.assertTrue((destination / "src/contextExport.test.ts").is_file())
        self.assertFalse((destination / "node_modules").exists())
        self.assertEqual(
            [entry[0] for entry in self.trace],
            ["process", "doctor", "process", "process"],
        )
        self.assertEqual(
            self.trace[0][2], ("npm-lock", "npm-install", "git-init", "git-add")
        )
        self.assertEqual(self.trace[2][2], ("npm-check", "npm-e2e"))
        self.assertEqual(self.trace[3][2], ("git-commit",))
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=destination,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "main",
        )

    def test_failed_generation_leaves_no_public_destination_or_sibling_stage(self):
        """A failing check must never leave a partially generated repository."""
        destination = self.workspace / "recipes"
        generator = AppGenerator(
            ROOT,
            artifact_builder=build_ui,
            doctor=RecordingDoctor(self.trace),
            process_runner=RecordingProcessRunner(self.trace, fail_label="npm-check"),
        )

        with self.assertRaisesRegex(AppGenerationError, "app creation failed"):
            generator.create(request(destination))

        self.assertFalse(destination.exists())
        self.assertFalse(
            any(path.name.startswith(".recipes.local-web-") for path in self.workspace.iterdir())
        )

    def test_generates_explicit_service_foundation_and_composes_its_release(self):
        destination = self.workspace / "recipes"
        composer = RecordingReleaseComposer()
        generator = AppGenerator(
            ROOT,
            artifact_builder=build_ui,
            doctor=RecordingDoctor(self.trace),
            process_runner=RecordingProcessRunner(self.trace),
            release_composer=composer,
        )

        generator.create(request(destination, kind="service"))

        manifest = json.loads((destination / "local-web.json").read_text())
        self.assertEqual(manifest["kind"], "service")
        self.assertTrue((destination / "server/service.mjs").is_file())
        self.assertEqual(len(composer.calls), 1)
        self.assertEqual(composer.calls[0][1].output, Path("release"))
        self.assertEqual(composer.calls[0][1].release_entries[0].target, Path("public"))

    def test_destination_collision_fails_before_any_build_or_process_work(self):
        """A pre-existing destination must remain untouched."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        (destination / "owner.txt").write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(AppGenerationError, "destination is unavailable"):
            self.generator.create(request(destination))

        self.assertEqual((destination / "owner.txt").read_text(encoding="utf-8"), "preserve\n")
        self.assertEqual(self.trace, [])

    def test_private_build_failure_has_a_stable_public_error(self):
        """A builder exception must not disclose an operator's filesystem detail."""
        destination = self.workspace / "recipes"

        def fail_build(_repository, _output):
            raise UiPackageError("private build detail /secret/path")

        generator = AppGenerator(
            ROOT,
            artifact_builder=fail_build,
            doctor=RecordingDoctor(self.trace),
            process_runner=RecordingProcessRunner(self.trace),
        )

        with self.assertRaisesRegex(AppGenerationError, "app creation failed") as raised:
            generator.create(request(destination))

        self.assertNotIn("private build detail", str(raised.exception))
        self.assertNotIn("/secret/path", str(raised.exception))
        self.assertFalse(destination.exists())

    def test_stage_creation_failure_has_a_stable_public_error(self):
        """A temporary-workspace failure must not expose its underlying path."""
        destination = self.workspace / "recipes"

        with patch(
            "local_web_server.app_generator.tempfile.mkdtemp",
            side_effect=OSError("private temporary directory path"),
        ), self.assertRaisesRegex(AppGenerationError, "app creation failed") as raised:
            self.generator.create(request(destination))

        self.assertNotIn("private temporary directory path", str(raised.exception))
        self.assertFalse(destination.exists())

    def test_node_module_cleanup_failure_aborts_publication(self):
        """A dependency-tree deletion failure must not publish a dirty app."""
        destination = self.workspace / "recipes"
        real_remove = shutil.rmtree

        def refuse_dependency_cleanup(path, *args, **kwargs):
            if Path(path).name == "node_modules":
                if kwargs.get("ignore_errors"):
                    return None
                raise OSError("private dependency tree path")
            return real_remove(path, *args, **kwargs)

        with patch(
            "local_web_server.app_generator.shutil.rmtree",
            side_effect=refuse_dependency_cleanup,
        ), self.assertRaisesRegex(AppGenerationError, "app creation failed") as raised:
            self.generator.create(request(destination))

        self.assertNotIn("private dependency tree path", str(raised.exception))
        self.assertFalse(destination.exists())
        self.assertFalse(
            any(path.name.startswith(".recipes.local-web-") for path in self.workspace.iterdir())
        )

    def test_failed_stage_cleanup_is_observable_without_private_details(self):
        """A cleanup error after a failed check must stay sanitized and observable."""
        destination = self.workspace / "recipes"
        generator = AppGenerator(
            ROOT,
            artifact_builder=build_ui,
            doctor=RecordingDoctor(self.trace),
            process_runner=RecordingProcessRunner(self.trace, fail_label="npm-check"),
        )
        real_remove = shutil.rmtree

        def refuse_stage_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".recipes.local-web-"):
                if kwargs.get("ignore_errors"):
                    return None
                raise OSError("private staged source path")
            return real_remove(path, *args, **kwargs)

        with patch(
            "local_web_server.app_generator.shutil.rmtree",
            side_effect=refuse_stage_cleanup,
        ), self.assertRaisesRegex(AppGenerationError, "app creation failed") as raised:
            generator.create(request(destination))

        self.assertNotIn("private staged source path", str(raised.exception))
        self.assertNotIn("npm-check failed", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, AppGenerationError)
        self.assertFalse(destination.exists())
        self.assertEqual(
            len(
                [
                    path
                    for path in self.workspace.iterdir()
                    if path.name.startswith(".recipes.local-web-")
                ]
            ),
            1,
        )

    def test_late_destination_collision_preserves_existing_repository(self):
        """A normal late collision must take the atomic no-replace failure path."""
        destination = self.workspace / "recipes"

        class LateCollisionRunner(RecordingProcessRunner):
            def run(inner_self, repository, commands):
                super().run(repository, commands)
                if any(command.label == "git-commit" for command in commands):
                    destination.mkdir()
                    (destination / "owner.txt").write_text("preserve\n", encoding="utf-8")

        generator = AppGenerator(
            ROOT,
            artifact_builder=build_ui,
            doctor=RecordingDoctor(self.trace),
            process_runner=LateCollisionRunner(self.trace),
        )

        with self.assertRaisesRegex(AppGenerationError, "destination is unavailable"):
            generator.create(request(destination))

        self.assertEqual((destination / "owner.txt").read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(
            any(path.name.startswith(".recipes.local-web-") for path in self.workspace.iterdir())
        )

    def test_preview_validates_and_keeps_the_destination_unmodified(self):
        """Invalid requests and dry runs must not create an application."""
        destination = self.workspace / "recipes"

        preview = self.generator.preview(request(destination))
        self.assertEqual(preview.destination, destination)
        self.assertFalse(destination.exists())
        with self.assertRaises(AppGenerationError):
            self.generator.preview(request(destination, app_id="Recipes"))
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_init_and_create_publish_equivalent_tracked_repositories(self):
        """A separate init renderer could drift from the create repository contract."""
        created = self.workspace / "created"
        initialised = self.workspace / "initialised"
        initialised.mkdir()

        create_result = self.generator.create(request(created))
        init_result = self.generator.init(request(initialised))

        self.assertEqual(create_result.destination, created)
        self.assertEqual(init_result.destination, initialised)
        self.assertEqual(self.tracked_content(created), self.tracked_content(initialised))
        self.assertFalse(
            any(
                path.name.startswith(".initialised.local-web-init-")
                for path in self.workspace.iterdir()
            )
        )

    def test_preview_init_validates_without_recovery_or_mutation(self):
        """A dry run must not consume recoverable siblings or write the destination."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        recovery_artifact = self.workspace / ".recipes.local-web-init-old.stage"
        recovery_artifact.mkdir()
        (recovery_artifact / "partial.txt").write_text("preserve\n", encoding="utf-8")

        preview = self.generator.preview_init(request(destination))

        self.assertEqual(preview.destination, destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(
            (recovery_artifact / "partial.txt").read_text(encoding="utf-8"),
            "preserve\n",
        )
        self.assertEqual(self.trace, [])

    def test_preview_init_refuses_absent_destination_without_consuming_backup(self):
        """A non-recovering preview must leave an interrupted backup untouched."""
        destination = self.workspace / "recipes"
        backup = self.workspace / ".recipes.local-web-init-old.empty"
        backup.mkdir()

        with self.assertRaisesRegex(
            AppGenerationError, "destination must be an existing directory"
        ):
            self.generator.preview_init(request(destination))

        self.assertFalse(destination.exists())
        self.assertTrue(backup.is_dir())
        self.assertEqual(list(backup.iterdir()), [])
        self.assertEqual(self.trace, [])

    def test_init_recovers_an_interrupted_empty_backup_before_generation(self):
        """Skipping recovery would leave a valid interrupted init unusable."""
        destination = self.workspace / "recipes"
        backup = self.workspace / ".recipes.local-web-init-old.empty"
        backup.mkdir()

        result = self.generator.init(request(destination))

        self.assertEqual(result.destination, destination)
        self.assertTrue((destination / "package.json").is_file())
        self.assertFalse(backup.exists())
        self.assertFalse(
            any(
                path.name.startswith(".recipes.local-web-init-")
                for path in self.workspace.iterdir()
            )
        )

    def test_init_generation_failures_restore_the_original_empty_destination(self):
        """Any failed shared-pipeline boundary must leave no partial public app."""
        cases = (
            ("artifact", None, False),
            ("npm-check", "npm-check", False),
            ("doctor", None, True),
            ("git-commit", "git-commit", False),
        )
        for name, fail_label, diagnostic in cases:
            with self.subTest(boundary=name):
                destination = self.workspace / name
                destination.mkdir()

                def artifact_builder(repository, output):
                    if name == "artifact":
                        raise UiPackageError("private artifact failure")
                    return build_ui(repository, output)

                generator = AppGenerator(
                    ROOT,
                    artifact_builder=artifact_builder,
                    doctor=RecordingDoctor(self.trace, diagnostic=diagnostic),
                    process_runner=RecordingProcessRunner(
                        self.trace, fail_label=fail_label
                    ),
                )

                with self.assertRaisesRegex(AppGenerationError, "app creation failed"):
                    generator.init(request(destination))

                self.assertTrue(destination.is_dir())
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse(
                    any(
                        path.name.startswith(f".{name}.local-web-init-")
                        for path in self.workspace.iterdir()
                    )
                )

    def test_init_first_publication_rename_failure_preserves_empty_destination(self):
        """Failure to move the original folder must not expose the staged app."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        real_rename = Path.rename

        def fail_destination_rename(path, target):
            if Path(path) == destination:
                raise OSError("private rename detail")
            return real_rename(path, target)

        with patch.object(Path, "rename", fail_destination_rename), self.assertRaisesRegex(
            AppGenerationError, "app creation failed"
        ) as raised:
            self.generator.init(request(destination))

        self.assertNotIn("private rename detail", str(raised.exception))
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(
            any(
                path.name.startswith(".recipes.local-web-init-")
                for path in self.workspace.iterdir()
            )
        )

    def test_init_second_publication_rename_failure_restores_empty_destination(self):
        """Failure to promote the complete stage must roll back the empty backup."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        real_rename = Path.rename

        def fail_stage_rename(path, target):
            if Path(path).name.endswith(".stage"):
                raise OSError("private rename detail")
            return real_rename(path, target)

        with patch.object(Path, "rename", fail_stage_rename), self.assertRaisesRegex(
            AppGenerationError, "app creation failed"
        ) as raised:
            self.generator.init(request(destination))

        self.assertNotIn("private rename detail", str(raised.exception))
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(
            any(
                path.name.startswith(".recipes.local-web-init-")
                for path in self.workspace.iterdir()
            )
        )

    @staticmethod
    def git_head(repository):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def tracked_content(repository):
        paths = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        return {
            path.decode("utf-8"): (repository / path.decode("utf-8")).read_bytes()
            for path in paths
            if path
        }


class AppGeneratorIntegrationTests(unittest.TestCase):
    @acceptance
    def test_real_doctor_and_process_runner_complete_the_generated_app_checks(self):
        """The generated repository must retain clean Git and lint policy gates."""
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as text:
            destination = Path(text) / "recipes"

            class ObservingProcessRunner(ProcessRunner):
                def __init__(self):
                    super().__init__()
                    self.calls = []
                    self.lint_results = []

                def lint(self, repository, source):
                    policy_file = repository / "src/tabler-policy.ts"
                    policy_file.write_text(source, encoding="utf-8")
                    result = subprocess.run(
                        ("npm", "run", "lint"),
                        cwd=repository,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.lint_results.append(
                        (result.returncode, result.stdout + result.stderr)
                    )

                def exercise_tabler_import_policy(self, repository):
                    self.lint(
                        repository,
                        "import { IconRocket } from '@tabler/icons-react';\n"
                        "export const directIcon = IconRocket;\n",
                    )
                    self.lint(
                        repository,
                        "import IconRocket from '@tabler/icons-react/dist/esm/icons/IconRocket';\n"
                        "export const subpathIcon = IconRocket;\n",
                    )
                    self.lint(
                        repository,
                        "// @tabler/icons-react/IconRocket is platform-owned.\n"
                        "export const note = '@tabler/icons-react';\n",
                    )
                    (repository / "src/tabler-policy.ts").unlink()

                def run(self, repository, commands):
                    self.calls.append(tuple(command.label for command in commands))
                    if tuple(command.label for command in commands) == (
                        "npm-check",
                        "npm-e2e",
                    ):
                        self.exercise_tabler_import_policy(repository)
                    super().run(repository, commands)

            runner = ObservingProcessRunner()
            result = AppGenerator(
                ROOT,
                doctor=AppDoctor(),
                process_runner=runner,
            ).create(request(destination))

            self.assertEqual(result.destination, destination)
            self.assertEqual(AppDoctor().inspect(destination).diagnostics, ())
            self.assertNotEqual(runner.lint_results[0][0], 0)
            self.assertIn("@tabler/icons-react", runner.lint_results[0][1])
            self.assertNotEqual(runner.lint_results[1][0], 0)
            self.assertIn("@tabler/icons-react", runner.lint_results[1][1])
            self.assertEqual(runner.lint_results[2][0], 0)
            status = subprocess.run(
                (
                    "git",
                    "-c",
                    "core.excludesfile=/dev/null",
                    "status",
                    "--porcelain",
                ),
                cwd=destination,
                env={
                    **os.environ,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            self.assertEqual(
                runner.calls,
                [
                    ("npm-lock", "npm-install", "git-init", "git-add"),
                    ("npm-check", "npm-e2e"),
                    ("git-commit",),
                ],
            )


if __name__ == "__main__":
    unittest.main()
