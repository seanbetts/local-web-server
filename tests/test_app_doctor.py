import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server import app_doctor
from local_web_server.app_doctor import AppDoctor
from local_web_server.app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    AppProvenance,
    UiArtifactReference,
    render_provenance,
)
from local_web_server.app_template import TemplateInputs, render_template


class AppDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "recipes"
        self.repository.mkdir()
        artifact = b"deterministic-ui"
        digest = hashlib.sha256(artifact).hexdigest()
        inputs = TemplateInputs(
            app_id="recipes",
            title="Recipe Collection",
            route="/recipes",
            icon="book",
            accent="#8EA7C6",
            ui_version="0.1.0",
            ui_sha256=digest,
            capabilities=(),
        )
        for rendered in render_template(inputs):
            path = self.repository / rendered.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered.content, encoding="utf-8")
        vendor = self.repository / "vendor/local-web-ui.tgz"
        vendor.parent.mkdir()
        vendor.write_bytes(artifact)
        provenance = AppProvenance(
            schema_version=1,
            template_version=CURRENT_TEMPLATE_VERSION,
            platform_contract_version=1,
            ui=UiArtifactReference("0.1.0", digest),
            capabilities=(),
            domain_palette_tokens=(),
            managed_files=(
                Path("AGENTS.md"),
                Path("local-web.json"),
                vendor.relative_to(self.repository),
            ),
        )
        (self.repository / ".local-web-platform.json").write_bytes(
            render_provenance(provenance)
        )
        self.adoption_files = {
            Path("AGENTS.md"): (self.repository / "AGENTS.md").read_bytes(),
            Path("local-web.json"): (self.repository / "local-web.json").read_bytes(),
            Path(".local-web-platform.json"): (
                self.repository / ".local-web-platform.json"
            ).read_bytes(),
            Path("vendor/local-web-ui.tgz"): vendor.read_bytes(),
        }
        self.git("init", "--initial-branch=main")
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "fixture",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *argv):
        return subprocess.run(
            ("git", "-c", "core.hooksPath=/dev/null", *argv),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

    def write_and_commit(self, relative, content):
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", relative)
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "mutate",
        )

    def codes(self):
        return [item.code for item in AppDoctor().inspect(self.repository).diagnostics]

    def test_git_boundary_disables_optional_locks_and_literal_pathspecs(self):
        completed = subprocess.CompletedProcess((), 0, b"", b"")
        with patch.object(app_doctor.subprocess, "run", return_value=completed) as run:
            app_doctor._git(self.repository, ("diff", "--quiet", "--", "path"))

        argv = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(argv[0:2], ("git", "--literal-pathspecs"))
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def make_legacy_commit(self):
        manifest = json.loads((self.repository / "local-web.json").read_text())
        manifest.pop("platform")
        (self.repository / "local-web.json").write_text(json.dumps(manifest))
        (self.repository / ".local-web-platform.json").unlink()
        (self.repository / "vendor/local-web-ui.tgz").unlink()
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "legacy",
        )

    def valid_adoption_replacements(self):
        return dict(self.adoption_files)

    def test_valid_generated_repository_has_no_diagnostics(self):
        report = AppDoctor().inspect(self.repository)

        self.assertEqual(report.app, "recipes")
        self.assertTrue(report.compatible)
        self.assertEqual(report.diagnostics, ())

    def test_doctor_does_not_parse_application_source_languages(self):
        for relative in (
            "src/broken.ts",
            "src/broken.tsx",
            "src/broken.css",
            "vite.extra.ts",
            "samplealpha_tracker/broken.py",
        ):
            self.write_and_commit(relative, "<<< deliberately invalid >>>\n")

        self.assertEqual(AppDoctor().inspect(self.repository).diagnostics, ())

    def test_generated_service_command_with_repository_data_is_structurally_compatible(self):
        service_inputs = TemplateInputs(
            app_id="recipes",
            title="Recipe Collection",
            route="/recipes",
            icon="book",
            accent="#8EA7C6",
            ui_version="0.1.0",
            ui_sha256=hashlib.sha256(b"deterministic-ui").hexdigest(),
            capabilities=(),
            kind="service",
        )
        manifest = json.loads(
            next(
                rendered.content
                for rendered in render_template(service_inputs)
                if rendered.path == Path("local-web.json")
            )
        )
        manifest["service"]["startCommand"] = [
            "/usr/bin/env",
            "node",
            "{release}/server/service.mjs",
            "--port",
            "{port}",
            "--data-dir",
            "{repository}/data",
        ]
        self.write_and_commit("local-web.json", json.dumps(manifest))

        report = AppDoctor().inspect(self.repository)

        self.assertTrue(report.compatible)
        self.assertEqual(report.diagnostics, ())

    def test_unborn_generated_repository_uses_staged_index_as_clean_authority(self):
        import shutil

        shutil.rmtree(self.repository / ".git")
        self.git("init", "--initial-branch=main")
        self.git("add", "--all")

        report = AppDoctor().inspect(self.repository)

        self.assertTrue(report.compatible)
        self.assertEqual(report.diagnostics, ())

        (self.repository / "AGENTS.md").write_text(
            "changed after staging", encoding="utf-8"
        )
        self.assertIn("git.managed-files-dirty", self.codes())

    def test_legacy_manifest_is_valid_but_not_clean(self):
        manifest = json.loads((self.repository / "local-web.json").read_text())
        manifest.pop("platform")
        (self.repository / "local-web.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.repository / ".local-web-platform.json").unlink()
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "legacy",
        )

        report = AppDoctor().inspect(self.repository)

        self.assertTrue(report.compatible)
        self.assertEqual(
            [item.code for item in report.diagnostics], ["platform.legacy"]
        )
        self.assertEqual(report.diagnostics[0].severity, "warning")
        self.assertEqual(
            report.diagnostics[0].remedy,
            "Run local-web app update --repository .",
        )

    def test_updateable_platform_diagnostics_use_supported_repository_command(self):
        updateable_codes = (
            "platform.legacy",
            "platform.template-version-mismatch",
            "ui.version-mismatch",
            "platform.capabilities-mismatch",
            "ui.artifact-missing",
            "ui.digest-mismatch",
        )

        for code in updateable_codes:
            with self.subTest(code=code):
                self.assertEqual(
                    app_doctor._diagnostic(code, "private-recipes").remedy,
                    "Run local-web app update --repository .",
                )

    def test_contract_mismatch_does_not_claim_the_updater_can_migrate_it(self):
        diagnostic = app_doctor._diagnostic(
            "platform.contract-version-mismatch", "private-recipes"
        )

        self.assertEqual(
            diagnostic.remedy,
            "Restore manifest and provenance to the supported platform contract.",
        )

    def test_candidate_overlay_validates_adoption_without_mutating_public_doctor(self):
        self.make_legacy_commit()
        replacements = self.valid_adoption_replacements()

        candidate = AppDoctor().inspect_candidate(
            self.repository,
            replacements,
            frozenset(replacements),
        )

        self.assertTrue(candidate.compatible)
        self.assertEqual(candidate.diagnostics, ())
        self.assertEqual(
            [item.code for item in AppDoctor().inspect(self.repository).diagnostics],
            ["platform.legacy"],
        )

    def test_candidate_overlay_reports_structural_errors_from_replacement_bytes(self):
        self.make_legacy_commit()
        replacements = self.valid_adoption_replacements()
        replacements[Path("package.json")] = b"{}\n"

        report = AppDoctor().inspect_candidate(
            self.repository,
            replacements,
            frozenset(replacements),
        )

        self.assertIn("package.invalid", [item.code for item in report.diagnostics])

    def test_reports_manifest_provenance_disagreement_and_incompatible_versions(self):
        manifest = json.loads((self.repository / "local-web.json").read_text())
        manifest["platform"]["templateVersion"] = 1
        manifest["platform"]["uiVersion"] = "0.2.0"
        self.write_and_commit("local-web.json", json.dumps(manifest))

        self.assertEqual(
            self.codes(),
            ["platform.template-version-mismatch", "ui.version-mismatch"],
        )

        manifest["platform"]["contractVersion"] = 2
        self.write_and_commit("local-web.json", json.dumps(manifest))
        self.assertEqual(self.codes(), ["platform.contract-version-mismatch"])

    def test_reports_missing_and_changed_vendored_artifact_without_leaking_bytes(self):
        (self.repository / "vendor/local-web-ui.tgz").unlink()
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "missing",
        )
        self.assertIn("ui.artifact-missing", self.codes())

        (self.repository / "vendor/local-web-ui.tgz").write_bytes(
            b"private artifact bytes"
        )
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "changed",
        )
        report = AppDoctor().inspect(self.repository)
        diagnostic = next(
            item for item in report.diagnostics if item.code == "ui.digest-mismatch"
        )
        self.assertEqual(
            diagnostic.message, "Vendored UI package does not match provenance."
        )
        self.assertEqual(
            diagnostic.remedy, "Run local-web app update --repository ."
        )
        self.assertNotIn("private artifact bytes", repr(report))

    def test_reports_dirty_managed_file_but_ignores_untracked_env(self):
        (self.repository / "AGENTS.md").write_text(
            "dirty private instructions", encoding="utf-8"
        )
        (self.repository / ".env").write_text("SECRET=do-not-read", encoding="utf-8")

        report = AppDoctor().inspect(self.repository)

        self.assertIn(
            "git.managed-files-dirty", [item.code for item in report.diagnostics]
        )
        self.assertNotIn("do-not-read", repr(report))

    def test_reports_missing_standard_script_and_never_recurses_through_check(self):
        package = json.loads((self.repository / "package.json").read_text())
        del package["scripts"]["test:e2e"]
        self.write_and_commit("package.json", json.dumps(package))
        self.assertIn("package.script-missing", self.codes())

        package["scripts"]["test:e2e"] = "npm run check"
        self.write_and_commit("package.json", json.dumps(package))
        self.assertIn("package.script-recursive", self.codes())

    def test_reports_an_app_check_script_that_invokes_local_web(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"]["check"] = "local-web app doctor --repository . && npm test"
        self.write_and_commit("package.json", json.dumps(package))

        self.assertIn("package.script-platform-coupled", self.codes())

    def test_accepts_extended_indirect_and_lifecycle_verification_scripts(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"].update(
            {
                "precheck": "npm run generate:fixtures",
                "check": "npm run lint && npm run test && npm run test:python && npm run build",
                "test:python": ".venv/bin/python -m pytest",
                "generate:fixtures": "node scripts/generate-fixtures.mjs",
            }
        )
        self.write_and_commit("package.json", json.dumps(package))

        self.assertEqual(self.codes(), [])

    def test_reports_recursive_check_in_an_indirect_script(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"]["lint"] = "npm run lint-inner"
        package["scripts"]["lint-inner"] = "npm run check"
        self.write_and_commit("package.json", json.dumps(package))

        self.assertIn("package.script-recursive", self.codes())

    def test_reports_platform_coupling_through_option_bearing_indirection(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"].update(
            {
                "check": "npm --silent run platform:doctor",
                "platform:doctor": "local-web app doctor --repository .",
            }
        )
        self.write_and_commit("package.json", json.dumps(package))

        self.assertIn("package.script-platform-coupled", self.codes())

    def test_reports_platform_coupling_in_reachable_lifecycle_script(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"]["postcheck"] = "local-web status"
        self.write_and_commit("package.json", json.dumps(package))

        self.assertIn("package.script-platform-coupled", self.codes())

    def test_ignores_unrelated_scripts_that_reference_platform_or_check_commands(self):
        package = json.loads((self.repository / "package.json").read_text())
        package["scripts"].update(
            {
                "ci": "npm run check && npm run test:e2e",
                "platform:status": "local-web status",
                "preprecheck": "local-web status",
                "postprecheck": "npm run check",
            }
        )
        self.write_and_commit("package.json", json.dumps(package))

        self.assertEqual(self.codes(), [])

    def test_reports_missing_reserved_theme_link(self):
        self.write_and_commit(
            "index.html",
            '<html><head><link rel="stylesheet" href="/theme.css"></head><body></body></html>\n',
        )

        self.assertIn("theme.link-invalid", self.codes())

    def test_theme_link_attribute_order_does_not_change_the_contract(self):
        html = (self.repository / "index.html").read_text()
        html = html.replace(
            'rel="stylesheet" href="/_local-web/platform/theme.css"',
            'href="/_local-web/platform/theme.css" rel="stylesheet"',
        )
        self.write_and_commit("index.html", html)

        self.assertNotIn("theme.link-invalid", self.codes())

    def test_ignores_arbitrary_typescript_css_and_vite_source(self):
        mutations = {
            "vite.config.ts": "private vite source without platform markers\n",
            "src/App.tsx": (
                "import { IconRocket } from '@tabler/icons-react';\n"
                "export const App = () => <button style={{color:'#123456'}}>bad</button>;\n"
            ),
            "src/icon-module.ts": "export * from '@tabler/icons-react';\n",
            "src/app.css": (
                ":root { --lwp-colour-canvas: #FFFFFF; }\n"
                "button { background: #123456; }\n"
            ),
        }
        for relative, content in mutations.items():
            self.write_and_commit(relative, content)

        self.assertEqual(self.codes(), [])

    def test_refuses_tracked_manifest_and_provenance_symlinks(self):
        private_manifest = self.repository.parent / "private-manifest.json"
        private_manifest.write_bytes((self.repository / "local-web.json").read_bytes())
        (self.repository / "local-web.json").unlink()
        (self.repository / "local-web.json").symlink_to(private_manifest)
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "manifest symlink",
        )
        self.assertEqual(self.codes(), ["manifest.invalid"])

        (self.repository / "local-web.json").unlink()
        (self.repository / "local-web.json").write_bytes(private_manifest.read_bytes())
        private_provenance = self.repository.parent / "private-provenance.json"
        private_provenance.write_bytes(
            (self.repository / ".local-web-platform.json").read_bytes()
        )
        (self.repository / ".local-web-platform.json").unlink()
        (self.repository / ".local-web-platform.json").symlink_to(private_provenance)
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Doctor Test",
            "-c",
            "user.email=doctor@test.invalid",
            "commit",
            "-m",
            "provenance symlink",
        )
        self.assertEqual(self.codes(), ["provenance.invalid"])

    def test_refuses_symlinked_vendored_artifact_without_reading_it(self):
        external = self.repository.parent / "private-external"
        external.mkdir()
        vendor = self.repository / "vendor"
        external_vendor = external / "vendor"
        vendor.rename(external_vendor)
        vendor.symlink_to(external_vendor, target_is_directory=True)

        codes = self.codes()
        self.assertIn("ui.artifact-missing", codes)
        self.assertNotIn("ui.digest-mismatch", codes)

    def test_malformed_json_becomes_sanitised_stable_diagnostic(self):
        self.write_and_commit("local-web.json", '{"private":"secret",')

        report = AppDoctor().inspect(self.repository)

        self.assertEqual(report.app, "recipes")
        self.assertEqual(
            [item.code for item in report.diagnostics], ["manifest.invalid"]
        )
        self.assertNotIn("private", repr(report))
        self.assertNotIn(str(self.repository), repr(report))


if __name__ == "__main__":
    unittest.main()
