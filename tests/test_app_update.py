import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server import app_update
from local_web_server.app_doctor import AppDoctor, Diagnostic, DoctorReport
from local_web_server.app_foundation import render_react_vite_foundation
from local_web_server.app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    AppProvenance,
    UiArtifactReference,
    parse_provenance,
    render_provenance,
)
from local_web_server.app_update import (
    AppUpdateError,
    AppUpdatePlanChangedError,
    AppUpdater,
    NpmLockfileBuilder,
)
from local_web_server.app_template import TemplateInputs
from local_web_server.app_update_models import AppUpdatePlan, AppUpdateRequest, FileChange
from local_web_server.app_update_transaction import UpdatePublisher
from local_web_server.process_runner import ProcessCommand, ProcessRunError, ProcessRunner
from local_web_server.ui_package import (
    UiPackageArtifact,
    UiPackageError,
    build_ui_package,
)
from tests.suites import acceptance


ROOT = Path(__file__).parents[1]
UI_BYTES = b"reviewed-ui-package"
UI_SHA256 = hashlib.sha256(UI_BYTES).hexdigest()
LOCK_BYTES = b'{"lockfileVersion":3,"name":"legacy"}\n'
THEME_LINK = b'<link rel="stylesheet" href="/_local-web/platform/theme.css">'
LEGACY_COLOUR_MODE_BOOTSTRAP = (
    b"    <script>\n"
    b"      (() => {\n"
    b"        try {\n"
    b"          const mode = localStorage.getItem('local-web:colour-mode');\n"
    b"          if (mode === 'light' || mode === 'dark') {\n"
    b"            document.documentElement.dataset.lwpColourMode = mode;\n"
    b"          } else {\n"
    b"            delete document.documentElement.dataset.lwpColourMode;\n"
    b"          }\n"
    b"        } catch {\n"
    b"          delete document.documentElement.dataset.lwpColourMode;\n"
    b"        }\n"
    b"      })();\n"
    b"    </script>\n"
)
FRONTEND_PATHS = {
    Path("eslint.config.js"), Path("index.html"), Path("package.json"),
    Path("playwright.config.ts"), Path("tsconfig.app.json"),
    Path("tsconfig.json"), Path("tsconfig.node.json"), Path("vite.config.ts"),
    Path("src/App.tsx"), Path("src/App.test.tsx"), Path("src/app.css"),
    Path("src/contextExport.ts"), Path("src/contextExport.test.ts"),
    Path("src/main.tsx"), Path("src/platform.ts"),
    Path("src/platform.test.ts"), Path("src/test/setup.ts"),
    Path("tests/app.spec.ts"),
}
FOUNDATION_PLANNED_PATHS = {
    Path(".local-web-platform.json"), Path("AGENTS.md"),
    Path("local-web.json"), Path("package-lock.json"),
    Path("vendor/local-web-ui.tgz"), *FRONTEND_PATHS,
}
FOUNDATION_CONFLICT_PATHS = FOUNDATION_PLANNED_PATHS - {
    Path("AGENTS.md"), Path("local-web.json"),
}
NESTED_FOUNDATION_PATHS = {
    path for path in FOUNDATION_CONFLICT_PATHS if len(path.parts) > 1
}


def _legacy_manifest() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "id": "legacy",
        "title": "Legacy App",
        "route": "/legacy",
        "kind": "static",
        "build": {
            "commands": [["npm", "run", "build"]],
            "output": "dist",
            "environment": [],
        },
        "healthPath": "/legacy/",
        "home": {"icon": "app", "accent": "#8EA7C6"},
    }


def _package() -> dict[str, object]:
    return {
        "name": "legacy",
        "private": True,
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "lint": "eslint .",
            "test": "vitest run",
            "test:e2e": "playwright test",
        },
        "dependencies": {"react": "19.2.8"},
        "devDependencies": {"vite": "8.2.0"},
        "custom": {"preserve": True},
    }


def _service_manifest() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "id": "samplealpha-planning",
        "title": "Example Planning",
        "route": "/samplealpha-planning",
        "kind": "service",
        "build": {
            "commands": [["python3", "scripts/build.py"]],
            "output": ".local-web-dist",
            "environment": ["FINANCE_MODE"],
        },
        "healthPath": "/samplealpha-planning/healthz",
        "service": {
            "module": "server/service.py",
            "internalHealthPath": "/healthz",
            "frontendOutput": "public",
            "proxyPaths": ["/api", "/exports"],
            "startCommand": [
                "/usr/bin/env", "python3", "{release}/server/service.py",
                "--port", "{port}", "--data-dir", "{repository}/data",
            ],
        },
        "home": {"icon": "chart-line", "accent": "#74D3A4"},
    }


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _snapshot_tree(repository: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }


def _candidate_bytes(plan) -> dict[Path, bytes]:
    return {change.path: change.after for change in plan.changes}


def _planned_or_existing(plan, repository: Path, path: Path) -> bytes:
    return _candidate_bytes(plan).get(path, (repository / path).read_bytes())


class _ArtifactBuilder:
    def __init__(self, content=UI_BYTES, version="0.5.2"):
        self.content = content
        self.version = version

    def __call__(self, _repository, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.content)
        return UiPackageArtifact(
            self.version,
            hashlib.sha256(self.content).hexdigest(),
            output,
        )


class _LockfileBuilder:
    def __init__(self, content=LOCK_BYTES):
        self.content = content
        self.calls = []

    def build(self, package, current_lockfile, artifact):
        self.calls.append((package, current_lockfile, artifact))
        return self.content


class _Inspector:
    def __init__(self, diagnostics=()):
        self.diagnostics = diagnostics
        self.calls = []

    def inspect_candidate(self, repository, replacements, added_tracked):
        self.calls.append((repository, dict(replacements), added_tracked))
        return DoctorReport(
            "legacy",
            not any(item.severity == "error" for item in self.diagnostics),
            self.diagnostics,
        )


class _FoundationRunner:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.snapshots = []

    def run(self, repository, commands):
        self.calls.append((repository, commands))
        self.snapshots.append(_snapshot_tree(repository))
        if self.error is not None:
            raise self.error


class _SequencedInspector(_Inspector):
    def __init__(self, reports):
        super().__init__()
        self.reports = iter(reports)

    def inspect_candidate(self, repository, replacements, added_tracked):
        self.calls.append((repository, dict(replacements), added_tracked))
        return next(self.reports)


class _InterruptAfterFirstReplace:
    def __init__(self):
        self.calls = 0

    def __call__(self, source, destination):
        self.calls += 1
        if self.calls == 2:
            raise KeyboardInterrupt
        os.replace(source, destination)


class _CorruptFinalReplace:
    def __call__(self, source, destination):
        os.replace(source, destination)
        if destination.name == "local-web-ui.tgz":
            destination.write_bytes(b"corrupted after publication")


class AppUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.repository = Path(self.temporary.name) / "legacy"
        self.repository.mkdir()
        (self.repository / "local-web.json").write_bytes(_json_bytes(_legacy_manifest()))
        (self.repository / "package.json").write_bytes(_json_bytes(_package()))
        (self.repository / "index.html").write_bytes(
            b"<!doctype html>\n<html><head><title>Legacy</title></head><body></body></html>\n"
        )
        subprocess.run(
            ("git", "init", "--initial-branch=main"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", "add", "--all"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@localhost",
                "commit",
                "-m",
                "legacy",
            ),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        self.lockfile_builder = _LockfileBuilder()
        self.inspector = _Inspector()
        self.updater = AppUpdater(
            ROOT,
            artifact_builder=_ArtifactBuilder(),
            lockfile_builder=self.lockfile_builder,
            inspector=self.inspector,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def apply_plan(self, plan):
        for change in plan.changes:
            destination = self.repository / change.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(change.after)

    def commit_repository(self, message="update"):
        subprocess.run(
            ("git", "add", "--all"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@localhost",
                "commit",
                "-m",
                message,
            ),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

    def current_platform(self, *, capabilities=()):
        plan = self.updater.preview(
            AppUpdateRequest(self.repository, capabilities=capabilities)
        )
        self.apply_plan(plan)
        self.commit_repository("platform")
        return plan

    def make_current_platform_commit(self, *, managed_files=()):
        self.current_platform()
        if managed_files:
            provenance_path = self.repository / ".local-web-platform.json"
            provenance = parse_provenance(provenance_path.read_text(encoding="utf-8"))
            for managed in managed_files:
                (self.repository / managed).parent.mkdir(parents=True, exist_ok=True)
                (self.repository / managed).write_text("tracked\n", encoding="utf-8")
            provenance_path.write_bytes(
                render_provenance(
                    AppProvenance(
                        schema_version=provenance.schema_version,
                        template_version=provenance.template_version,
                        platform_contract_version=provenance.platform_contract_version,
                        ui=provenance.ui,
                        capabilities=provenance.capabilities,
                        domain_palette_tokens=provenance.domain_palette_tokens,
                        managed_files=tuple(
                            sorted(
                                (*provenance.managed_files, *managed_files),
                                key=lambda item: item.as_posix(),
                            )
                        ),
                    )
                )
            )
            self.commit_repository("managed files")

    def test_preview_adopts_legacy_app_without_writing_repository(self):
        before = _snapshot_tree(self.repository)

        plan = self.updater.preview(
            AppUpdateRequest(self.repository, capabilities=("supabase",))
        )

        self.assertEqual(plan.mode, "adopt")
        self.assertEqual(plan.capabilities, ("supabase",))
        self.assertEqual(plan.target_ui_version, "0.5.2")
        self.assertEqual(plan.target_ui_sha256, UI_SHA256)
        self.assertEqual(
            tuple(change.path.as_posix() for change in plan.changes),
            (
                ".local-web-platform.json",
                "index.html",
                "local-web.json",
                "package-lock.json",
                "package.json",
                "vendor/local-web-ui.tgz",
            ),
        )
        self.assertEqual(_snapshot_tree(self.repository), before)

    def test_preview_builds_ui_in_a_platform_sibling_temporary_directory(self):
        observed_outputs = []

        def build_artifact(_repository, output):
            observed_outputs.append(output)
            self.assertEqual(output.parent.parent, ROOT.parent)
            return _ArtifactBuilder()(ROOT, output)

        updater = AppUpdater(
            ROOT,
            artifact_builder=build_artifact,
            lockfile_builder=self.lockfile_builder,
            inspector=self.inspector,
        )

        plan = updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "adopt")
        self.assertEqual(len(observed_outputs), 1)

    def test_preview_refuses_pending_recovery_without_writing(self):
        plan = self.updater.preview(AppUpdateRequest(self.repository))
        interrupted = UpdatePublisher(replace=_InterruptAfterFirstReplace())
        with self.assertRaises(KeyboardInterrupt):
            interrupted.publish(self.repository, plan.changes, lambda: None)
        before = _snapshot_tree(self.repository)

        with self.assertRaisesRegex(AppUpdateError, "recovery required"):
            self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertTrue(UpdatePublisher().recovery_pending(self.repository))

    def test_update_recovers_before_replanning_and_publishes(self):
        plan = self.updater.preview(AppUpdateRequest(self.repository))
        interrupted = UpdatePublisher(replace=_InterruptAfterFirstReplace())
        with self.assertRaises(KeyboardInterrupt):
            interrupted.publish(self.repository, plan.changes, lambda: None)

        result = self.updater.update(AppUpdateRequest(self.repository))

        self.assertTrue(result.recovered)
        self.assertEqual(result.plan.mode, "adopt")
        self.assertEqual(
            {
                change.path: (self.repository / change.path).read_bytes()
                for change in result.plan.changes
            },
            {change.path: change.after for change in result.plan.changes},
        )
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))

    def test_update_expected_plan_refuses_fresh_plan_drift_before_publication(self):
        expected = self.updater.preview(AppUpdateRequest(self.repository))
        before = _snapshot_tree(self.repository)
        changed = AppUpdatePlan(
            **{**expected.__dict__, "target_ui_sha256": "f" * 64}
        )

        with patch.object(self.updater, "preview", return_value=changed):
            with self.assertRaisesRegex(
                AppUpdatePlanChangedError, "application update plan changed"
            ):
                self.updater.update(
                    AppUpdateRequest(self.repository), expected_plan=expected
                )

        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))

    def test_update_rolls_back_when_post_publication_doctor_rejects_candidate(self):
        before = _snapshot_tree(self.repository)
        rejected = Diagnostic("package.invalid", "error", "private", "private")
        inspector = _SequencedInspector(
            (
                DoctorReport("legacy", True, ()),
                DoctorReport("legacy", False, (rejected,)),
            )
        )
        updater = AppUpdater(
            ROOT,
            artifact_builder=_ArtifactBuilder(),
            lockfile_builder=self.lockfile_builder,
            inspector=inspector,
        )

        with self.assertRaisesRegex(AppUpdateError, "application update failed") as raised:
            updater.update(AppUpdateRequest(self.repository))

        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))
        self.assertNotIn("private", str(raised.exception))

    def test_update_rolls_back_when_published_bytes_do_not_match_plan(self):
        before = _snapshot_tree(self.repository)
        updater = AppUpdater(
            ROOT,
            artifact_builder=_ArtifactBuilder(),
            lockfile_builder=self.lockfile_builder,
            inspector=self.inspector,
            publisher=UpdatePublisher(replace=_CorruptFinalReplace()),
        )

        with self.assertRaisesRegex(AppUpdateError, "application update failed"):
            updater.update(AppUpdateRequest(self.repository))

        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))

    def test_preview_adds_only_the_reviewed_structural_contract(self):
        original_manifest = json.loads((self.repository / "local-web.json").read_text())
        original_package = json.loads((self.repository / "package.json").read_text())

        plan = self.updater.preview(AppUpdateRequest(self.repository))
        candidate = _candidate_bytes(plan)

        manifest = json.loads(candidate[Path("local-web.json")])
        self.assertEqual(
            manifest["platform"],
            {
                "contractVersion": 1,
                "templateVersion": 3,
                "uiVersion": "0.5.2",
                "capabilities": [],
            },
        )
        self.assertEqual(
            {key: value for key, value in manifest.items() if key != "platform"},
            original_manifest,
        )
        provenance = parse_provenance(
            candidate[Path(".local-web-platform.json")].decode("utf-8")
        )
        self.assertEqual(
            provenance.managed_files,
            (Path("local-web.json"), Path("vendor/local-web-ui.tgz")),
        )
        self.assertEqual(provenance.domain_palette_tokens, ())
        package = json.loads(candidate[Path("package.json")])
        self.assertEqual(
            package["dependencies"]["@local-web/ui"],
            "file:vendor/local-web-ui.tgz",
        )
        self.assertEqual(package["dependencies"]["react"], "19.2.8")
        self.assertEqual(
            package["scripts"]["check"],
            "npm run lint && npm run test && npm run build",
        )
        self.assertEqual(package["scripts"]["dev"], original_package["scripts"]["dev"])
        self.assertEqual(package["custom"], original_package["custom"])
        self.assertEqual(
            package["devDependencies"], original_package["devDependencies"]
        )
        self.assertEqual(candidate[Path("vendor/local-web-ui.tgz")], UI_BYTES)
        self.assertEqual(candidate[Path("index.html")].count(THEME_LINK), 1)

    def test_preview_rejects_capabilities_for_existing_platform_app(self):
        self.current_platform()

        with self.assertRaisesRegex(
            AppUpdateError, "application contract conflicts with update"
        ):
            self.updater.preview(
                AppUpdateRequest(self.repository, capabilities=("supabase",))
            )

    def test_preview_refresh_preserves_platform_owned_declarations(self):
        self.current_platform(capabilities=("supabase",))
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["platform"]["uiVersion"] = "0.4.0"
        manifest_path.write_bytes(_json_bytes(manifest))
        artifact_path = self.repository / "vendor/local-web-ui.tgz"
        artifact_path.write_bytes(b"older-ui")
        provenance_path = self.repository / ".local-web-platform.json"
        provenance = parse_provenance(provenance_path.read_text())
        provenance_path.write_bytes(
            render_provenance(
                AppProvenance(
                    schema_version=provenance.schema_version,
                    template_version=provenance.template_version,
                    platform_contract_version=provenance.platform_contract_version,
                    ui=UiArtifactReference(
                        version="0.4.0",
                        sha256=hashlib.sha256(b"older-ui").hexdigest(),
                    ),
                    capabilities=("supabase",),
                    domain_palette_tokens=("brand-accent",),
                    managed_files=(
                        Path("local-web.json"),
                        Path("src/theme.ts"),
                        Path("vendor/local-web-ui.tgz"),
                    ),
                )
            )
        )
        self.commit_repository("older platform")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        candidate = _candidate_bytes(plan)
        updated_manifest = json.loads(candidate[Path("local-web.json")])
        updated_provenance = parse_provenance(
            candidate[Path(".local-web-platform.json")].decode()
        )
        self.assertEqual(plan.mode, "refresh")
        self.assertEqual(plan.current_ui_version, "0.4.0")
        self.assertEqual(plan.capabilities, ("supabase",))
        self.assertEqual(updated_manifest["platform"]["capabilities"], ["supabase"])
        self.assertEqual(updated_provenance.domain_palette_tokens, ("brand-accent",))
        self.assertEqual(
            updated_provenance.managed_files,
            (
                Path("local-web.json"),
                Path("src/theme.ts"),
                Path("vendor/local-web-ui.tgz"),
            ),
        )

    def test_preview_refreshes_readable_template_disagreement(self):
        self.current_platform()
        provenance_path = self.repository / ".local-web-platform.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["templateVersion"] = 1
        provenance_path.write_bytes(_json_bytes(provenance))
        self.commit_repository("template disagreement")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        updated_manifest = json.loads(
            _planned_or_existing(
                plan, self.repository, Path("local-web.json")
            )
        )
        updated_provenance = parse_provenance(
            _planned_or_existing(
                plan, self.repository, Path(".local-web-platform.json")
            ).decode("utf-8")
        )
        self.assertEqual(plan.mode, "refresh")
        self.assertEqual(updated_manifest["platform"]["templateVersion"], 3)
        self.assertEqual(updated_provenance.template_version, 3)

    def test_preview_removes_one_exact_legacy_bootstrap_and_preserves_other_html_bytes(self):
        self.current_platform()
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["platform"]["templateVersion"] = 2
        manifest_path.write_bytes(_json_bytes(manifest))
        provenance_path = self.repository / ".local-web-platform.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["templateVersion"] = 2
        provenance_path.write_bytes(_json_bytes(provenance))
        original = (
            b"<!doctype html>\n<html><head data-head=\"keep\">\n"
            b"<!-- keep-before -->\n"
            + THEME_LINK
            + b"\n"
            + LEGACY_COLOUR_MODE_BOOTSTRAP
            + b"<!-- keep-after -->\n</head>\n"
            + b"<body data-body=\"keep\"><main>Keep this body byte-for-byte.</main></body>\n"
            + b"</html>\n"
        )
        (self.repository / "index.html").write_bytes(original)
        self.commit_repository("tracked version two bootstrap")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        candidate = _candidate_bytes(plan)
        self.assertEqual(
            _planned_or_existing(plan, self.repository, Path("index.html")),
            original.replace(LEGACY_COLOUR_MODE_BOOTSTRAP, b""),
        )
        self.assertEqual(
            json.loads(candidate[Path("local-web.json")])["platform"]["templateVersion"],
            3,
        )
        self.assertEqual(
            parse_provenance(candidate[Path(".local-web-platform.json")].decode()).template_version,
            3,
        )

    def test_preview_rejects_two_exact_legacy_bootstraps(self):
        self.current_platform()
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["platform"]["templateVersion"] = 2
        manifest_path.write_bytes(_json_bytes(manifest))
        provenance_path = self.repository / ".local-web-platform.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["templateVersion"] = 2
        provenance_path.write_bytes(_json_bytes(provenance))
        (self.repository / "index.html").write_bytes(
            b"<!doctype html><html><head>"
            + THEME_LINK
            + b"\n"
            + LEGACY_COLOUR_MODE_BOOTSTRAP
            + LEGACY_COLOUR_MODE_BOOTSTRAP
            + b"</head><body>Keep body.</body></html>"
        )
        self.commit_repository("tracked duplicate version two bootstraps")

        with self.assertRaisesRegex(
            AppUpdateError, "application contract conflicts with update"
        ):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refreshes_readable_ui_version_disagreement(self):
        self.current_platform()
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["platform"]["uiVersion"] = "0.4.0"
        manifest_path.write_bytes(_json_bytes(manifest))
        self.commit_repository("ui disagreement")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        updated_manifest = json.loads(
            _planned_or_existing(
                plan, self.repository, Path("local-web.json")
            )
        )
        updated_provenance = parse_provenance(
            _planned_or_existing(
                plan, self.repository, Path(".local-web-platform.json")
            ).decode("utf-8")
        )
        self.assertEqual(plan.mode, "refresh")
        self.assertEqual(updated_manifest["platform"]["uiVersion"], "0.5.2")
        self.assertEqual(updated_provenance.ui.version, "0.5.2")

    def test_preview_refresh_uses_manifest_capabilities_when_provenance_disagrees(self):
        self.current_platform()
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["platform"]["capabilities"] = ["supabase"]
        manifest_path.write_bytes(_json_bytes(manifest))
        self.commit_repository("capability disagreement")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        updated_provenance = parse_provenance(
            _planned_or_existing(
                plan, self.repository, Path(".local-web-platform.json")
            ).decode("utf-8")
        )
        self.assertEqual(plan.mode, "refresh")
        self.assertEqual(plan.capabilities, ("supabase",))
        self.assertEqual(updated_provenance.capabilities, ("supabase",))

    def test_preview_rejects_unsupported_contract_migration(self):
        self.current_platform()
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["platform"]["contractVersion"] = 2
        manifest_path.write_bytes(_json_bytes(manifest))
        self.commit_repository("unsupported contract")

        with self.assertRaisesRegex(
            AppUpdateError, "application update planning failed"
        ):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_reports_current_for_byte_identical_candidate(self):
        self.current_platform()

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "current")
        self.assertEqual(plan.changes, ())

    def test_preview_rejects_incompatible_existing_ui_dependency(self):
        package = _package()
        package["dependencies"]["@local-web/ui"] = "^0.5.2"
        (self.repository / "package.json").write_bytes(_json_bytes(package))
        self.commit_repository("incompatible dependency")

        with self.assertRaisesRegex(
            AppUpdateError, "application contract conflicts with update"
        ):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_rejects_ui_dependency_outside_dependencies(self):
        for bucket in (
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            for declaration in ("file:vendor/local-web-ui.tgz", "^0.5.2"):
                with self.subTest(bucket=bucket, declaration=declaration):
                    package = _package()
                    package.setdefault(bucket, {})["@local-web/ui"] = declaration
                    (self.repository / "package.json").write_bytes(
                        _json_bytes(package)
                    )
                    self.commit_repository(f"ui dependency in {bucket}")

                    with self.assertRaisesRegex(
                        AppUpdateError, "application contract conflicts with update"
                    ):
                        self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_preserves_all_unrelated_dependency_buckets_with_canonical_ui(self):
        package = _package()
        package["dependencies"]["@local-web/ui"] = (
            "file:vendor/local-web-ui.tgz"
        )
        package["optionalDependencies"] = {"optional-package": "1.0.0"}
        package["peerDependencies"] = {"peer-package": "2.0.0"}
        (self.repository / "package.json").write_bytes(_json_bytes(package))
        self.commit_repository("canonical ui dependency")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        updated_package = json.loads(
            _planned_or_existing(
                plan, self.repository, Path("package.json")
            )
        )
        self.assertEqual(
            updated_package["dependencies"]["@local-web/ui"],
            "file:vendor/local-web-ui.tgz",
        )
        self.assertEqual(
            updated_package["devDependencies"], package["devDependencies"]
        )
        self.assertEqual(
            updated_package["optionalDependencies"],
            package["optionalDependencies"],
        )
        self.assertEqual(
            updated_package["peerDependencies"], package["peerDependencies"]
        )

    def test_preview_requires_component_scripts_before_adding_check(self):
        for missing in ("lint", "test", "build"):
            with self.subTest(missing=missing):
                package = _package()
                del package["scripts"][missing]
                (self.repository / "package.json").write_bytes(_json_bytes(package))
                self.commit_repository(f"missing {missing}")
                with self.assertRaisesRegex(
                    AppUpdateError, "application contract conflicts with update"
                ):
                    self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_preserves_existing_check_script(self):
        package = _package()
        package["scripts"]["check"] = "npm run verify:all"
        package["scripts"]["verify:all"] = "npm run lint && npm run test && npm run build"
        (self.repository / "package.json").write_bytes(_json_bytes(package))
        self.commit_repository("custom check")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        package_candidate = json.loads(_candidate_bytes(plan)[Path("package.json")])
        self.assertEqual(package_candidate["scripts"]["check"], "npm run verify:all")

    def test_preview_accepts_the_generated_self_closing_theme_link(self):
        self.current_platform()
        generated_link = THEME_LINK[:-1] + b" />"
        index_path = self.repository / "index.html"
        index_path.write_bytes(index_path.read_bytes().replace(THEME_LINK, generated_link))
        self.commit_repository("generated theme link")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertNotIn(Path("index.html"), _candidate_bytes(plan))

    def test_preview_rejects_malformed_or_duplicate_reserved_theme_links(self):
        invalid_documents = (
            b'<html><head><link href="/_local-web/platform/theme.css"></head></html>',
            b"<html><head>" + THEME_LINK + THEME_LINK + b"</head></html>",
            b"<html><head></head><head></head></html>",
            b"<html><body></body></html>",
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                (self.repository / "index.html").write_bytes(document)
                self.commit_repository("invalid theme")
                with self.assertRaisesRegex(
                    AppUpdateError, "application contract conflicts with update"
                ):
                    self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_rejects_platform_declaration_without_readable_provenance(self):
        manifest = _legacy_manifest()
        manifest["platform"] = {
            "contractVersion": 1,
            "templateVersion": 1,
            "uiVersion": "0.4.0",
            "capabilities": [],
        }
        (self.repository / "local-web.json").write_bytes(_json_bytes(manifest))
        self.commit_repository("platform without provenance")

        with self.assertRaisesRegex(
            AppUpdateError, "application contract conflicts with update"
        ):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_sanitizes_malformed_manifest_and_provenance_errors(self):
        private_detail = f"secret bytes {self.repository}".encode()
        (self.repository / "local-web.json").write_bytes(b'{"secret":"' + private_detail)
        with self.assertRaises(AppUpdateError) as raised:
            self.updater.preview(AppUpdateRequest(self.repository))
        self.assertNotIn("secret bytes", str(raised.exception))
        self.assertNotIn(str(self.repository), str(raised.exception))

        (self.repository / "local-web.json").write_bytes(_json_bytes(_legacy_manifest()))
        self.current_platform()
        (self.repository / ".local-web-platform.json").write_bytes(
            b'{"secret":"' + private_detail
        )
        with self.assertRaises(AppUpdateError) as raised:
            self.updater.preview(AppUpdateRequest(self.repository))
        self.assertNotIn("secret bytes", str(raised.exception))
        self.assertNotIn(str(self.repository), str(raised.exception))

    def test_preview_rejects_candidate_doctor_errors(self):
        diagnostic = Diagnostic("package.invalid", "error", "private", "private")
        updater = AppUpdater(
            ROOT,
            artifact_builder=_ArtifactBuilder(),
            lockfile_builder=self.lockfile_builder,
            inspector=_Inspector((diagnostic,)),
        )

        with self.assertRaisesRegex(AppUpdateError, "application update planning failed"):
            updater.preview(AppUpdateRequest(self.repository))

    def test_preview_sanitizes_ui_artifact_build_errors(self):
        def fail_build(_repository, _output):
            raise UiPackageError(f"private artifact detail {self.repository}")

        updater = AppUpdater(
            ROOT,
            artifact_builder=fail_build,
            lockfile_builder=self.lockfile_builder,
            inspector=self.inspector,
        )

        with self.assertRaisesRegex(
            AppUpdateError, "application update planning failed"
        ) as raised:
            updater.preview(AppUpdateRequest(self.repository))
        self.assertNotIn("private artifact detail", str(raised.exception))
        self.assertNotIn(str(self.repository), str(raised.exception))

    def test_preview_preserves_unrelated_dirty_files(self):
        notes = self.repository / "private-notes.md"
        tracked = self.repository / "README.md"
        tracked.write_text("committed\n", encoding="utf-8")
        self.commit_repository("notes")
        notes.write_text("keep me", encoding="utf-8")
        tracked.write_text("local change\n", encoding="utf-8")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "adopt")
        self.assertEqual(notes.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(tracked.read_text(encoding="utf-8"), "local change\n")

    def test_preview_refuses_dirty_target_file(self):
        (self.repository / "package.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_staged_target_file(self):
        self._stage_package()

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_deleted_target_file(self):
        self._delete_index()

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_renamed_target_file(self):
        self._rename_package()

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_untracked_target_file(self):
        self._add_lockfile()

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_ignored_untracked_target_file(self):
        (self.repository / ".gitignore").write_text(
            "package-lock.json\n", encoding="utf-8"
        )
        self.commit_repository("ignore lockfile")
        self._add_lockfile()

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_allows_an_absent_target_under_an_ignored_parent_directory(self):
        (self.repository / ".gitignore").write_text("vendor/\n", encoding="utf-8")
        (self.repository / "vendor").mkdir()
        (self.repository / "vendor" / "unrelated.txt").write_text(
            "ignore me\n", encoding="utf-8"
        )
        self.commit_repository("ignore vendor")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "adopt")

    def test_refresh_refuses_dirty_current_managed_file(self):
        managed = Path("AGENTS.md")
        self.make_current_platform_commit(managed_files=(managed,))
        (self.repository / managed).write_text("local change\n", encoding="utf-8")

        with self.assertRaisesRegex(AppUpdateError, "managed files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_refuses_ignored_legacy_artifact_as_dirty_target(self):
        artifact = self.repository / "vendor" / "local-web-ui.tgz"
        artifact.parent.mkdir()
        artifact.write_bytes(UI_BYTES)
        (self.repository / ".gitignore").write_text(
            "vendor/local-web-ui.tgz\n", encoding="utf-8"
        )
        self.commit_repository("ignore legacy artifact")

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_refresh_refuses_ignored_untracked_provenance_as_dirty_target(self):
        self.current_platform()
        (self.repository / ".gitignore").write_text(
            ".local-web-platform.json\n", encoding="utf-8"
        )
        subprocess.run(
            ("git", "rm", "--cached", ".local-web-platform.json"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        self.commit_repository("untrack provenance")

        with self.assertRaisesRegex(AppUpdateError, "target files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_refresh_treats_managed_pathspec_magic_as_a_literal_path(self):
        magic = Path(":(glob)*.md")
        ordinary = self.repository / "ordinary.md"
        self.make_current_platform_commit(managed_files=(magic,))
        ordinary.write_text("committed\n", encoding="utf-8")
        self.commit_repository("ordinary markdown")
        ordinary.write_text("unrelated dirty change\n", encoding="utf-8")

        plan = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "current")
        (self.repository / magic).write_text("managed change\n", encoding="utf-8")
        with self.assertRaisesRegex(AppUpdateError, "managed files are not clean"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_real_doctor_does_not_conflate_literal_managed_pathspec_magic(self):
        magic = Path(":(glob)*.md")
        ordinary = self.repository / "ordinary.md"
        self.make_current_platform_commit(managed_files=(magic,))
        ordinary.write_text("committed\n", encoding="utf-8")
        self.commit_repository("ordinary markdown")
        ordinary.write_text("unrelated dirty change\n", encoding="utf-8")
        updater = AppUpdater(
            ROOT,
            artifact_builder=_ArtifactBuilder(),
            lockfile_builder=self.lockfile_builder,
            inspector=AppDoctor(),
        )

        plan = updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(plan.mode, "current")

    def test_preview_requires_a_clean_git_root_with_a_head(self):
        subprocess.run(
            ("git", "update-ref", "-d", "HEAD"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

        with self.assertRaisesRegex(AppUpdateError, "application update planning failed"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_preview_rejects_a_nested_directory_instead_of_the_repository_root(self):
        nested = self.repository / "nested"
        nested.mkdir()

        with self.assertRaisesRegex(AppUpdateError, "application update planning failed"):
            self.updater.preview(AppUpdateRequest(nested))

    def test_git_inspection_disables_optional_locks_and_literal_pathspecs(self):
        completed = subprocess.CompletedProcess((), 0, b"", b"")
        with patch.object(app_update.subprocess, "run", return_value=completed) as run:
            app_update._git(self.repository, ("status", "--porcelain=v1"))

        argv = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(argv[0:2], ("git", "--literal-pathspecs"))
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def _stage_package(self):
        (self.repository / "package.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ("git", "add", "package.json"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

    def _delete_index(self):
        (self.repository / "index.html").unlink()

    def _rename_package(self):
        (self.repository / "package.json").rename(self.repository / "renamed-package.json")

    def _add_lockfile(self):
        (self.repository / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def test_preview_validates_request_boundary(self):
        invalid = (
            object(),
            AppUpdateRequest("not-a-path"),
            AppUpdateRequest(self.repository, capabilities=["supabase"]),
            AppUpdateRequest(self.repository, capabilities=("unsupported",)),
            AppUpdateRequest(self.repository, foundation="browser-native"),
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaisesRegex(
                AppUpdateError, "application update request is invalid"
            ):
                self.updater.preview(request)


class FoundationAppUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.root = Path(self.temporary.name)
        self.repository = self._create_repository("samplealpha")
        self.lockfile_builder = _LockfileBuilder()
        self.inspector = _Inspector()
        self.foundation_runner = _FoundationRunner()
        self.updater = self._updater()

    def tearDown(self):
        self.temporary.cleanup()

    def _create_repository(self, name):
        repository = self.root / name
        repository.mkdir()
        (repository / "local-web.json").write_bytes(
            _json_bytes(_service_manifest())
        )
        (repository / "server").mkdir()
        (repository / "server" / "service.py").write_bytes(
            b"SERVICE_SENTINEL = 'private backend bytes'\n"
        )
        (repository / "samplealpha_tracker" / "web_assets").mkdir(parents=True)
        (repository / "samplealpha_tracker" / "web_assets" / "index.html").write_bytes(
            b"private legacy frontend bytes\n"
        )
        (repository / "data").mkdir()
        (repository / "data" / "accounts.sqlite").write_bytes(
            b"private repository data bytes\n"
        )
        subprocess.run(
            ("git", "init", "--initial-branch=main"),
            cwd=repository,
            check=True,
            capture_output=True,
        )
        # Detached maintenance can remove its lock while copytree clones fixtures.
        subprocess.run(
            ("git", "config", "--local", "maintenance.auto", "false"),
            cwd=repository,
            check=True,
            capture_output=True,
        )
        self._commit(repository, "samplealpha service")
        return repository

    def _copy_repository(self, name, source=None):
        return Path(
            shutil.copytree(source or self.repository, self.root / name, symlinks=True)
        )

    @staticmethod
    def _write_target(repository, target, content=b"private target bytes\n"):
        destination = repository / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def _target_state_repository(self, index, target, state, tracked_base):
        if state == "tracked":
            repository = tracked_base
            destination = repository / target
        elif state in {"deleted", "unreadable"}:
            repository = self._copy_repository(
                f"matrix-{index}-{state}", tracked_base
            )
            destination = repository / target
            if state == "deleted":
                destination.unlink()
            else:
                destination.chmod(0)
        else:
            repository = self._copy_repository(f"matrix-{index}-{state}")
            destination = repository / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            if state == "ignored":
                exclude = repository / ".git" / "info" / "exclude"
                exclude.write_text(
                    exclude.read_text(encoding="utf-8")
                    + f"/{target.as_posix()}\n",
                    encoding="utf-8",
                )
                destination.write_bytes(b"private ignored target bytes\n")
            elif state == "staged":
                destination.write_bytes(b"private staged target bytes\n")
                subprocess.run(
                    ("git", "add", "--", target.as_posix()),
                    cwd=repository,
                    check=True,
                    capture_output=True,
                )
            elif state == "symlinked":
                destination.symlink_to(repository / "local-web.json")
            elif state == "directory":
                destination.mkdir()
            else:
                destination.write_bytes(b"private untracked target bytes\n")
        self._assert_target_state(repository, target, state)
        return repository

    def _assert_target_state(self, repository, target, state):
        tracked = subprocess.run(
            ("git", "ls-files", "--error-unmatch", "--", target.as_posix()),
            cwd=repository,
            capture_output=True,
        ).returncode == 0
        status = subprocess.run(
            (
                "git", "status", "--porcelain=v1", "--ignored",
                "--", target.as_posix(),
            ),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        destination = repository / target
        if state == "tracked":
            self.assertTrue(tracked)
            self.assertEqual(status, b"")
        elif state == "untracked":
            self.assertFalse(tracked)
            self.assertTrue(status.startswith(b"?? "))
        elif state == "ignored":
            self.assertFalse(tracked)
            self.assertTrue(status.startswith(b"!! "))
        elif state == "staged":
            self.assertTrue(tracked)
            self.assertTrue(status.startswith(b"A  "))
        elif state == "deleted":
            self.assertTrue(tracked)
            self.assertTrue(status.startswith(b" D "))
        elif state == "symlinked":
            self.assertFalse(tracked)
            self.assertTrue(destination.is_symlink())
        elif state == "directory":
            self.assertFalse(tracked)
            self.assertTrue(destination.is_dir())
        elif state == "unreadable":
            self.assertTrue(tracked)
            self.assertEqual(destination.stat().st_mode & 0o777, 0)

    @staticmethod
    def _commit(repository, message):
        subprocess.run(
            ("git", "add", "--all"),
            cwd=repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git", "-c", "user.name=Test", "-c",
                "user.email=test@localhost", "commit", "-m", message,
            ),
            cwd=repository,
            check=True,
            capture_output=True,
        )

    def _updater(self, **overrides):
        values = {
            "artifact_builder": _ArtifactBuilder(),
            "lockfile_builder": self.lockfile_builder,
            "inspector": self.inspector,
            "process_runner": self.foundation_runner,
        }
        values.update(overrides)
        return AppUpdater(ROOT, **values)

    def _request(self, repository=None, *, capabilities=()):
        return AppUpdateRequest(
            repository or self.repository,
            capabilities=capabilities,
            foundation="react-vite",
        )

    def test_fixture_disables_automatic_git_maintenance(self):
        configured = subprocess.run(
            ("git", "config", "--local", "--get", "maintenance.auto"),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

        self.assertEqual(configured.stdout, b"false\n")

    def test_foundation_request_reads_manifest_before_missing_frontend_files(self):
        plan = self.updater.preview(self._request())

        self.assertEqual(plan.mode, "adopt")
        self.assertEqual(plan.foundation, "react-vite")
        self.assertIsNone(plan.current_platform_contract_version)
        self.assertIsNone(plan.current_template_version)
        self.assertIsNone(plan.current_ui_version)
        self.assertEqual(plan.target_platform_contract_version, 1)
        self.assertEqual(plan.target_template_version, CURRENT_TEMPLATE_VERSION)

        with self.assertRaisesRegex(AppUpdateError, "planning failed"):
            self.updater.preview(AppUpdateRequest(self.repository))

    def test_foundation_preview_is_pure_and_preserves_the_service_contract(self):
        before = _snapshot_tree(self.repository)
        manifest_before = json.loads(
            (self.repository / "local-web.json").read_text()
        )

        plan = self.updater.preview(
            self._request(capabilities=("supabase",))
        )

        self.assertEqual(_snapshot_tree(self.repository), before)
        candidate = _candidate_bytes(plan)
        manifest_after = json.loads(candidate[Path("local-web.json")])
        expected = dict(manifest_before)
        expected["platform"] = {
            "contractVersion": 1,
            "templateVersion": CURRENT_TEMPLATE_VERSION,
            "uiVersion": "0.5.2",
            "capabilities": ["supabase"],
        }
        self.assertEqual(manifest_after, expected)
        self.assertEqual(
            manifest_after["service"]["startCommand"],
            manifest_before["service"]["startCommand"],
        )
        self.assertEqual(
            set(candidate),
            {
                Path(".local-web-platform.json"), Path("AGENTS.md"),
                Path("local-web.json"), Path("package-lock.json"),
                Path("vendor/local-web-ui.tgz"), *FRONTEND_PATHS,
            },
        )

    def test_foundation_preview_preserves_existing_agent_guidance(self):
        guidance = b"existing private guidance with `service`\n"
        (self.repository / "AGENTS.md").write_bytes(guidance)
        self._commit(self.repository, "agent guidance")
        before = _snapshot_tree(self.repository)

        plan = self.updater.preview(self._request())

        self.assertNotIn(Path("AGENTS.md"), _candidate_bytes(plan))
        self.assertEqual((self.repository / "AGENTS.md").read_bytes(), guidance)
        self.assertEqual(_snapshot_tree(self.repository), before)
        provenance = parse_provenance(
            _candidate_bytes(plan)[Path(".local-web-platform.json")].decode()
        )
        self.assertEqual(
            provenance.managed_files,
            (Path("local-web.json"), Path("vendor/local-web-ui.tgz")),
        )

    def test_foundation_previews_are_deterministic_and_isolated(self):
        first = self.updater.preview(self._request())
        second = self.updater.preview(self._request())

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(change.path for change in first.changes),
            tuple(sorted((change.path for change in first.changes), key=str)),
        )
        self.assertEqual(
            self.lockfile_builder.calls,
            [
                (_candidate_bytes(first)[Path("package.json")], None, UI_BYTES),
                (_candidate_bytes(second)[Path("package.json")], None, UI_BYTES),
            ],
        )
        expected_commands = (
            ProcessCommand("npm-install", ("npm", "ci", "--ignore-scripts")),
            ProcessCommand("npm-check", ("npm", "run", "check")),
        )
        self.assertEqual(
            [commands for _workspace, commands in self.foundation_runner.calls],
            [expected_commands, expected_commands],
        )
        expected_workspace = {
            *(path.as_posix() for path in FRONTEND_PATHS),
            "local-web.json", "package-lock.json", "vendor/local-web-ui.tgz",
        }
        self.assertEqual(
            [set(snapshot) for snapshot in self.foundation_runner.snapshots],
            [expected_workspace, expected_workspace],
        )
        for snapshot in self.foundation_runner.snapshots:
            self.assertNotIn("server/service.py", snapshot)
            self.assertNotIn("samplealpha_tracker/web_assets/index.html", snapshot)
            self.assertNotIn("data/accounts.sqlite", snapshot)
            self.assertNotIn(".local-web-platform.json", snapshot)

    def test_foundation_preview_sanitizes_temporary_verification_failure(self):
        private = f"private npm output {self.repository}"
        updater = self._updater(
            process_runner=_FoundationRunner(ProcessRunError(private))
        )

        with self.assertRaisesRegex(
            AppUpdateError, "application update planning failed"
        ) as raised:
            updater.preview(self._request())

        self.assertNotIn("private npm output", str(raised.exception))
        self.assertNotIn(str(self.repository), str(raised.exception))

    def test_foundation_preview_rejects_existing_platform(self):
        manifest = _service_manifest()
        manifest["platform"] = {
            "contractVersion": 1, "templateVersion": 1,
            "uiVersion": "0.5.2", "capabilities": [],
        }
        (self.repository / "local-web.json").write_bytes(_json_bytes(manifest))
        self._commit(self.repository, "existing platform")
        with self.assertRaisesRegex(
            AppUpdateError, "application contract conflicts with update"
        ):
            self.updater.preview(self._request(capabilities=("supabase",)))

    def test_foundation_preview_refuses_every_target_and_each_required_state(self):
        baseline = self.updater.preview(self._request())
        self.assertEqual(
            {change.path for change in baseline.changes},
            FOUNDATION_PLANNED_PATHS,
        )
        for index, target in enumerate(sorted(FOUNDATION_CONFLICT_PATHS, key=str)):
            with self.subTest(target=target.as_posix(), state="untracked"):
                repository = self._target_state_repository(
                    index, target, "untracked", self.repository
                )
                with self.assertRaisesRegex(
                    AppUpdateError, "application contract conflicts with update"
                ) as raised:
                    self.updater.preview(self._request(repository))
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn(str(repository), str(raised.exception))

        state_cases = (
            (Path("package.json"), "tracked"),
            (Path("src/App.tsx"), "untracked"),
            (Path("vendor/local-web-ui.tgz"), "ignored"),
            (Path("vite.config.ts"), "staged"),
            (Path("src/platform.ts"), "deleted"),
            (Path(".local-web-platform.json"), "symlinked"),
            (Path("tests/app.spec.ts"), "directory"),
            (Path("package-lock.json"), "unreadable"),
        )
        dirty_states = {"staged", "deleted", "unreadable"}
        for index, (target, state) in enumerate(state_cases):
            with self.subTest(target=target.as_posix(), state=state):
                tracked_base = self._copy_repository(f"state-{index}-tracked")
                if state in {"tracked", "deleted", "unreadable"}:
                    self._write_target(
                        tracked_base, target, b"private tracked target\n"
                    )
                    self._commit(tracked_base, f"track {target.as_posix()}")
                repository = self._target_state_repository(
                    len(FOUNDATION_CONFLICT_PATHS) + index,
                    target,
                    state,
                    tracked_base,
                )
                expected = (
                    "target files are not clean"
                    if state in dirty_states
                    else "application contract conflicts with update"
                )
                with self.assertRaisesRegex(AppUpdateError, expected) as raised:
                    self.updater.preview(self._request(repository))
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn(str(repository), str(raised.exception))

    def test_foundation_preview_refuses_unsafe_parents_for_each_nested_category(self):
        targets = (
            Path("src/App.tsx"),
            Path("tests/app.spec.ts"),
            Path("vendor/local-web-ui.tgz"),
        )
        self.assertEqual(
            {target.parts[0] for target in targets},
            {target.parts[0] for target in NESTED_FOUNDATION_PATHS},
        )
        for index, target in enumerate(targets):
            for parent_state in ("symlinked", "non-directory"):
                with self.subTest(
                    target=target.as_posix(), parent_state=parent_state
                ):
                    repository = self._copy_repository(
                        f"unsafe-{index}-{parent_state}"
                    )
                    parent = repository / target.parts[0]
                    if parent_state == "symlinked":
                        parent.symlink_to(
                            repository / "server", target_is_directory=True
                        )
                    else:
                        parent.write_bytes(b"private unsafe parent bytes\n")
                    with self.assertRaisesRegex(
                        AppUpdateError, "application contract conflicts with update"
                    ) as raised:
                        self.updater.preview(self._request(repository))
                    self.assertNotIn("private unsafe parent", str(raised.exception))
                    self.assertNotIn(str(repository), str(raised.exception))

    def test_foundation_preview_separately_refuses_partial_provenance_and_artifact(self):
        for index, target in enumerate(
            (Path(".local-web-platform.json"), Path("vendor/local-web-ui.tgz"))
        ):
            with self.subTest(target=target.as_posix()):
                repository = self._copy_repository(f"partial-{index}")
                self._write_target(repository, target, b"private partial bytes\n")
                with self.assertRaisesRegex(
                    AppUpdateError, "application contract conflicts with update"
                ) as raised:
                    self.updater.preview(self._request(repository))
                self.assertNotIn("private partial bytes", str(raised.exception))
                self.assertNotIn(str(repository), str(raised.exception))

    def test_foundation_preview_rejects_nested_git_directory(self):
        with self.assertRaisesRegex(
            AppUpdateError, "application update planning failed"
        ):
            self.updater.preview(self._request(self.repository / "server"))

    def test_foundation_preview_requires_git_head_and_refuses_pending_recovery(self):
        no_head = self._create_repository("no-head")
        subprocess.run(
            ("git", "update-ref", "-d", "HEAD"), cwd=no_head,
            check=True, capture_output=True,
        )
        with self.assertRaisesRegex(AppUpdateError, "planning failed"):
            self.updater.preview(self._request(no_head))

        plan = self.updater.preview(self._request())
        interrupted = UpdatePublisher(replace=_InterruptAfterFirstReplace())
        with self.assertRaises(KeyboardInterrupt):
            interrupted.publish(self.repository, plan.changes, lambda: None)
        before = _snapshot_tree(self.repository)
        with self.assertRaisesRegex(AppUpdateError, "recovery required"):
            self.updater.preview(self._request())
        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertTrue(UpdatePublisher().recovery_pending(self.repository))

    def test_foundation_update_publishes_exact_plan_and_preserves_unrelated_dirty_files(self):
        tracked = self.repository / "server" / "service.py"
        untracked = self.repository / "private-notes.md"
        tracked.write_bytes(b"private dirty backend\n")
        untracked.write_bytes(b"private untracked notes\n")
        before_owned = {
            path: (self.repository / path).read_bytes()
            for path in (
                Path("server/service.py"),
                Path("samplealpha_tracker/web_assets/index.html"),
                Path("data/accounts.sqlite"),
                Path("private-notes.md"),
            )
        }

        result = self.updater.update(self._request())

        self.assertEqual(result.plan.mode, "adopt")
        self.assertFalse(result.recovered)
        self.assertEqual(
            {change.path: (self.repository / change.path).read_bytes()
             for change in result.plan.changes},
            {change.path: change.after for change in result.plan.changes},
        )
        self.assertEqual(
            {path: (self.repository / path).read_bytes() for path in before_owned},
            before_owned,
        )
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))

    def test_foundation_update_rolls_back_and_recovers_nested_publication(self):
        before = _snapshot_tree(self.repository)
        rejected = Diagnostic("package.invalid", "error", "private", "private")
        rejecting = self._updater(
            inspector=_SequencedInspector(
                (
                    DoctorReport("samplealpha-planning", True, ()),
                    DoctorReport("samplealpha-planning", False, (rejected,)),
                )
            )
        )
        with self.assertRaisesRegex(AppUpdateError, "application update failed"):
            rejecting.update(self._request())
        self.assertEqual(_snapshot_tree(self.repository), before)
        self.assertFalse((self.repository / "src").exists())

        plan = self.updater.preview(self._request())
        interrupted = UpdatePublisher(replace=_InterruptAfterFirstReplace())
        with self.assertRaises(KeyboardInterrupt):
            interrupted.publish(self.repository, plan.changes, lambda: None)

        result = self.updater.update(self._request())

        self.assertTrue(result.recovered)
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))
        self.assertEqual(
            {change.path: (self.repository / change.path).read_bytes()
             for change in result.plan.changes},
            {change.path: change.after for change in result.plan.changes},
        )

    def test_normal_update_reports_current_after_committed_foundation_adoption(self):
        result = self.updater.update(self._request())
        self._commit(self.repository, "adopt foundation")

        current = self.updater.preview(AppUpdateRequest(self.repository))

        self.assertEqual(result.plan.mode, "adopt")
        self.assertEqual(current.mode, "current")
        self.assertIsNone(current.foundation)
        self.assertEqual(current.changes, ())

    @acceptance
    def test_rendered_foundation_temporary_verification_disables_lifecycle_scripts(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as text:
            temporary = Path(text)
            artifact_directory = temporary / "artifact"
            artifact_directory.mkdir()
            artifact = build_ui_package(
                ROOT, artifact_directory / "local-web-ui.tgz"
            )
            artifact_bytes = artifact.path.read_bytes()
            rendered = render_react_vite_foundation(
                TemplateInputs(
                    app_id="samplealpha-planning",
                    title="Example Planning",
                    route="/samplealpha-planning",
                    icon="chart-line",
                    accent="#74D3A4",
                    ui_version=artifact.version,
                    ui_sha256=artifact.sha256,
                    capabilities=(),
                    kind="service",
                ),
                include_reference_service=False,
            )
            candidates = {
                item.path: item.content.encode("utf-8") for item in rendered.files
            }
            package = json.loads(candidates[Path("package.json")])
            package["scripts"]["preinstall"] = (
                "node -e \"require('fs').writeFileSync('lifecycle-ran','ran')\""
            )
            candidates[Path("package.json")] = _json_bytes(package)
            candidates[Path("vendor/local-web-ui.tgz")] = artifact_bytes
            candidates[Path("package-lock.json")] = NpmLockfileBuilder().build(
                candidates[Path("package.json")], None, artifact_bytes
            )
            candidates[Path("local-web.json")] = _json_bytes(_service_manifest())
            changes = tuple(
                FileChange(path, None, candidates[path])
                for path in sorted(candidates, key=str)
            )
            workspace = temporary / "workspace"
            workspace.mkdir()

            app_update._verify_temporary_foundation(
                workspace,
                changes,
                frozenset(candidates),
                ProcessRunner(),
            )

            self.assertEqual(
                {item.path for item in rendered.files}, FRONTEND_PATHS
            )
            self.assertFalse((workspace / "lifecycle-ran").exists())

    def test_temporary_foundation_verification_rejects_nonbytes_change_target(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as text:
            workspace = Path(text)

            with self.assertRaisesRegex(AppUpdateError, "planning failed"):
                app_update._verify_temporary_foundation(
                    workspace,
                    (FileChange(Path("package.json"), None, None),),
                    frozenset((Path("package.json"),)),
                    _FoundationRunner(),
                )

    def test_published_candidate_verification_rejects_nonbytes_plan_target(self):
        plan = AppUpdatePlan(
            app_id="legacy",
            mode="refresh",
            current_ui_version="0.5.1",
            target_ui_version="0.5.2",
            target_ui_sha256=UI_SHA256,
            capabilities=(),
            changes=(FileChange(Path("index.html"), b"old", None),),
        )

        with self.assertRaisesRegex(AppUpdateError, "planning failed"):
            self.updater._verify_published_candidate(self.repository, plan)


def _npm_artifact(version="0.5.2", index=b"") -> bytes:
    package = json.dumps(
        {"name": "@local-web/ui", "version": version, "files": ["index.js"]},
        separators=(",", ":"),
    ).encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, content in (
                ("package/package.json", package),
                ("package/index.js", index),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class NpmLockfileBuilderTests(unittest.TestCase):
    def test_build_refreshes_stale_vendored_ui_lock_metadata(self):
        package = _json_bytes(
            {
                "name": "lock-test",
                "version": "1.0.0",
                "dependencies": {
                    "@local-web/ui": "file:vendor/local-web-ui.tgz"
                },
            }
        )
        builder = NpmLockfileBuilder()
        old_lockfile = builder.build(
            package,
            None,
            _npm_artifact(version="0.4.0", index=b"old implementation"),
        )
        new_artifact = _npm_artifact(
            version="0.5.2", index=b"reviewed implementation"
        )

        refreshed = builder.build(package, old_lockfile, new_artifact)

        ui_package = json.loads(refreshed)["packages"]["node_modules/@local-web/ui"]
        expected_integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(new_artifact).digest()
        ).decode("ascii")
        self.assertEqual(ui_package["version"], "0.5.2")
        self.assertEqual(ui_package["resolved"], "file:vendor/local-web-ui.tgz")
        self.assertEqual(ui_package["integrity"], expected_integrity)

    @acceptance
    def test_build_uses_package_only_install_and_disables_lifecycle_scripts(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as text:
            workspace = Path(text)
            sentinel = workspace / "lifecycle-ran"
            package = {
                "name": "lock-test",
                "version": "1.0.0",
                "scripts": {
                    "preinstall": (
                        "node -e \"require('fs').writeFileSync("
                        + json.dumps(str(sentinel))
                        + ",'ran')\""
                    )
                },
                "dependencies": {"@local-web/ui": "file:vendor/local-web-ui.tgz"},
            }

            lockfile = NpmLockfileBuilder().build(
                _json_bytes(package), None, _npm_artifact()
            )

            payload = json.loads(lockfile)
            self.assertEqual(
                payload["packages"][""]["dependencies"]["@local-web/ui"],
                "file:vendor/local-web-ui.tgz",
            )
            self.assertFalse(sentinel.exists())

    def test_build_sanitizes_process_and_malformed_lockfile_failures(self):
        class FailingRunner:
            def run(self, _repository, _commands):
                raise ProcessRunError("private npm output /secret/path")

        with self.assertRaisesRegex(
            AppUpdateError, "application lockfile generation failed"
        ) as raised:
            NpmLockfileBuilder(FailingRunner()).build(
                _json_bytes(
                    {
                        "name": "lock-test",
                        "dependencies": {"@local-web/ui": "file:vendor/local-web-ui.tgz"},
                    }
                ),
                None,
                _npm_artifact(),
            )
        self.assertNotIn("private npm output", str(raised.exception))
        self.assertNotIn("/secret/path", str(raised.exception))

        class MalformedLockfileRunner:
            def run(self, repository, _commands):
                (repository / "package-lock.json").write_bytes(b"private lock bytes")

        with self.assertRaisesRegex(
            AppUpdateError, "application lockfile generation failed"
        ) as raised:
            NpmLockfileBuilder(MalformedLockfileRunner()).build(
                _json_bytes(
                    {
                        "name": "lock-test",
                        "dependencies": {"@local-web/ui": "file:vendor/local-web-ui.tgz"},
                    }
                ),
                None,
                _npm_artifact(),
            )
        self.assertNotIn("private lock bytes", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
