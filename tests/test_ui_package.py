import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import local_web_server.ui_package as ui_package
from local_web_server.ui_package import (
    CURRENT_UI_PACKAGE_VERSION,
    UiPackageError,
    build_ui_package,
)
from tests.suites import acceptance


PUBLIC_DIST_FILES = {
    "AppShell.d.ts",
    "ContextExportButton.d.ts",
    "Icon.d.ts",
    "InteractiveExportControl.d.ts",
    "PlatformShell.d.ts",
    "SegmentedControl.d.ts",
    "ThemeControl.d.ts",
    "actions.d.ts",
    "colour.d.ts",
    "colourMode.d.ts",
    "content.d.ts",
    "contextExportDocument.d.ts",
    "contextExportModel.d.ts",
    "feedback.d.ts",
    "fallback-theme.css",
    "focus.d.ts",
    "forms.d.ts",
    "index.d.ts",
    "index.js",
    "interactiveExportDocument.js",
    "interactiveExportDocument.d.ts",
    "interactiveExportEnvironment.d.ts",
    "interactiveExportModel.d.ts",
    "interactiveExportVite.d.ts",
    "layout.d.ts",
    "overlays.d.ts",
    "styles.css",
    "vite.d.ts",
    "vite-env.d.ts",
    "vite.js",
}
_PRIVATE_TEST_PATH = "/Users/" + "private/path"


class UiPackageBuildTests(unittest.TestCase):

    def test_current_release_accepts_optional_vite_peer_metadata(self):
        metadata = (Path(__file__).parents[1] / "packages/ui/package.json").read_bytes()
        version, canonical = ui_package._canonical_metadata(metadata)
        self.assertEqual(version, CURRENT_UI_PACKAGE_VERSION)
        self.assertEqual(json.loads(canonical)["peerDependenciesMeta"], {"vite": {"optional": True}})


class UiPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def _repository(self, name: str = "repository") -> Path:
        repository = self.root / name
        package = repository / "packages/ui"
        (package / "src").mkdir(parents=True)
        (repository / "node_modules").mkdir()
        metadata = json.loads((Path(__file__).parents[1] / "packages/ui/package.json").read_text())
        (package / "package.json").write_text(json.dumps(metadata))
        (package / "src/index.ts").write_text("export {};\n")
        (package / "tsconfig.json").write_text("{}\n")
        (package / "vite.config.ts").write_text("export default {};\n")
        (package / "dist").mkdir()
        for name in PUBLIC_DIST_FILES:
            (package / "dist" / name).write_text("export {};\n")
        (repository / "package.json").write_text(json.dumps({
            "name": "ui-package-fixture", "version": "0.0.0", "private": True,
            "workspaces": ["packages/*"],
        }))
        (repository / "package-lock.json").write_text(json.dumps({
            "name": "ui-package-fixture", "version": "0.0.0", "lockfileVersion": 3,
            "requires": True, "packages": {
                "": {"name": "ui-package-fixture", "version": "0.0.0", "workspaces": ["packages/*"]},
                "packages/ui": {"name": "@local-web/ui", "version": metadata["version"]},
            },
        }))
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run([
            "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "fixture",
        ], cwd=repository, check=True)
        return repository


    def _build_fixture(self, repository: Path, output: Path, stage_mutator=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        def fake_build(snapshot):
            dist = snapshot.path / "packages" / "ui" / "dist"
            dist.mkdir()
            for name in PUBLIC_DIST_FILES:
                (dist / name).write_text("export {};\n", encoding="utf-8")
            if stage_mutator is not None:
                stage_mutator(dist)

        with patch("local_web_server.ui_package._run_build", side_effect=fake_build) as run:
            artifact = build_ui_package(repository, output)
        return artifact, run

    def test_builds_byte_identical_npm_compatible_archives(self):
        repository = self._repository()
        first, _ = self._build_fixture(repository, self.root / "one" / "local-web-ui.tgz")
        second, _ = self._build_fixture(repository, self.root / "two" / "local-web-ui.tgz")

        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.sha256, hashlib.sha256(first.path.read_bytes()).hexdigest())
        self.assertEqual(first.version, CURRENT_UI_PACKAGE_VERSION)
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o644)

        with gzip.GzipFile(fileobj=io.BytesIO(first.path.read_bytes()), mode="rb") as compressed:
            compressed.peek(1)
            self.assertEqual(compressed.mtime, 0)
            with tarfile.open(fileobj=compressed, mode="r:") as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                self.assertEqual(names, sorted(names))
                self.assertEqual(
                    names,
                    [
                        "package",
                        "package/dist",
                        *(f"package/dist/{name}" for name in sorted(PUBLIC_DIST_FILES)),
                        "package/package.json",
                    ],
                )
                for member in members:
                    self.assertTrue(member.name == "package" or member.name.startswith("package/"))
                    self.assertEqual(member.uid, 0)
                    self.assertEqual(member.gid, 0)
                    self.assertEqual(member.uname, "")
                    self.assertEqual(member.gname, "")
                    self.assertEqual(member.mtime, 0)
                    self.assertEqual(
                        member.mode,
                        0o755 if member.isdir() else 0o644,
                    )
                    self.assertFalse(member.issym() or member.islnk())
                    self.assertFalse(member.name.endswith(".map"))


    def test_timeout_kills_delayed_descendant_before_it_can_mutate_staging_dist(self):
        source = Path(__file__).parents[1]
        repository = self.root / "timeout-clone"
        subprocess.run(
            ["git", "clone", "-q", os.fspath(source), os.fspath(repository)],
            check=True,
        )
        marker = self.root / "descendant-ran"
        fake_npm = self.root / "npm"
        fake_npm.write_text(
            "#!/bin/sh\n"
            f"( sleep 1; mkdir -p packages/ui/dist; printf EVIL > packages/ui/dist/index.js; touch {marker} ) &\n"
            "sleep 5\n",
            encoding="utf-8",
        )
        fake_npm.chmod(0o755)

        with patch("local_web_server.ui_package._resolve_npm", return_value=str(fake_npm)), patch(
            "local_web_server.ui_package._BUILD_TIMEOUT_SECONDS", 0.1
        ), self.assertRaisesRegex(UiPackageError, "UI package build failed"):
            build_ui_package(repository, self.root / "timeout.tgz")
        time.sleep(1.2)
        self.assertFalse(marker.exists())
        self.assertFalse((repository / "packages" / "ui" / "dist" / "EVIL").exists())

    def test_rejects_hostile_package_members_without_replacing_prior_artifact(self):
        repository = self._repository()
        output = self.root / "artifacts" / "local-web-ui.tgz"
        output.parent.mkdir()
        output.write_bytes(b"previous artifact")
        def add_private_file(dist):
            (dist / "private-key.js").write_text("not public", encoding="utf-8")

        with self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
            self._build_fixture(repository, output, add_private_file)

        self.assertEqual(output.read_bytes(), b"previous artifact")
        self.assertNotIn("private-key", str(caught.exception))

    def test_failed_ui_build_leaves_prior_artifact_untouched(self):
        repository = self._repository()
        output = self.root / "prior.tgz"
        output.write_bytes(b"previous artifact")

        with patch(
            "local_web_server.ui_package._run_build",
            side_effect=UiPackageError("UI package build failed"),
        ), self.assertRaisesRegex(UiPackageError, "UI package build failed"):
            build_ui_package(repository, output)

        self.assertEqual(output.read_bytes(), b"previous artifact")

    def test_rejects_source_maps_and_symlinks_in_the_public_dist_tree(self):
        for kind in ("map", "symlink"):
            with self.subTest(kind=kind):
                repository = self._repository(kind)
                def add_invalid_member(dist):
                    if kind == "map":
                        (dist / "index.js.map").write_text("{}", encoding="utf-8")
                    else:
                        os.symlink(dist / "index.js", dist / "linked.js")

                with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
                    self._build_fixture(
                        repository, self.root / f"{kind}.tgz", add_invalid_member
                    )

    def test_excludes_unexpected_package_source_files_from_the_public_archive(self):
        repository = self._repository()
        package = repository / "packages" / "ui"
        (package / "README.md").write_text("source-only", encoding="utf-8")

        artifact, _ = self._build_fixture(repository, self.root / "archive.tgz")

        with tarfile.open(artifact.path, mode="r:gz") as archive:
            self.assertNotIn("package/README.md", archive.getnames())

    def test_requires_complete_public_dist_exports_and_safe_metadata(self):
        repository = self._repository()
        metadata = repository / "packages" / "ui" / "package.json"
        valid = json.loads(metadata.read_text(encoding="utf-8"))

        invalid_cases = (
            {**valid, "version": "not-a-version"},
            {**valid, "files": ["dist", "src"]},
            {**valid, "exports": {".": "./dist/../../outside.js"}},
            {**valid, "exports": {".": "./dist/missing.js"}},
        )
        for candidate in invalid_cases:
            with self.subTest(candidate=candidate):
                metadata.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
                    self._build_fixture(repository, self.root / "invalid.tgz")
        metadata.write_text(json.dumps(valid), encoding="utf-8")

    def test_rejects_non_regular_dist_members(self):
        repository = self._repository()
        if not hasattr(os, "mkfifo"):
            self.skipTest("platform lacks fifo support")

        def add_fifo(dist):
            os.mkfifo(dist / "unexpected")

        with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
            self._build_fixture(repository, self.root / "fifo.tgz", add_fifo)

    def test_rejects_duplicate_or_non_public_package_metadata(self):
        repository = self._repository()
        metadata = repository / "packages" / "ui" / "package.json"
        duplicate = metadata.read_text(encoding="utf-8").replace(
            f'"version": "{CURRENT_UI_PACKAGE_VERSION}",',
            f'"version": "{CURRENT_UI_PACKAGE_VERSION}", "version": "0.4.0",',
        )
        for payload in (
            duplicate,
            json.dumps({**json.loads(metadata.read_text()), "authToken": "TOP-SECRET"}),
            json.dumps({**json.loads(metadata.read_text()), "scripts": {"preinstall": "echo bad"}}),
            json.dumps({**json.loads(metadata.read_text()), "exports": {".": "./dist/index.js", "import": "./dist/index.js"}}),
        ):
            with self.subTest(payload=payload[:60]):
                metadata.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
                    self._build_fixture(repository, self.root / "metadata.tgz")
                self.assertNotIn("TOP-SECRET", str(caught.exception))

    def test_rejects_protected_or_missing_output_parent_without_writing(self):
        repository = self._repository()
        protected = repository / "packages" / "ui" / "package.json"
        before = protected.read_bytes()
        with self.assertRaisesRegex(UiPackageError, "UI package output failed"):
            self._build_fixture(repository, protected)
        self.assertEqual(protected.read_bytes(), before)

        missing = self.root / "missing" / "artifact.tgz"
        with patch("local_web_server.ui_package._run_build"):
            with self.assertRaisesRegex(UiPackageError, "UI package output failed"):
                build_ui_package(repository, missing)
        self.assertFalse(missing.parent.exists())

    def test_rejects_hardlinks_and_sensitive_or_unexpected_dist_names(self):
        for name, create in (
            ("clientSecret.js", lambda path, dist: path.write_text("secret", encoding="utf-8")),
            ("public.js", lambda path, dist: os.link(dist / "index.js", path)),
            ("unexpected.js", lambda path, dist: path.write_text("extra", encoding="utf-8")),
        ):
            with self.subTest(name=name):
                repository = self._repository(name)
                def add_invalid_member(dist):
                    create(dist / name, dist)

                with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
                    self._build_fixture(
                        repository, self.root / f"{name}.tgz", add_invalid_member
                    )

    def test_sanitizes_ustar_and_subprocess_failures(self):
        repository = self._repository()
        def add_long_name(dist):
            (dist / ("a" * 240 + ".d.ts")).write_text("export {};", encoding="utf-8")

        with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
            self._build_fixture(repository, self.root / "long.tgz", add_long_name)

        with patch("local_web_server.ui_package._run_build", side_effect=UiPackageError("UI package build failed")):
            with self.assertRaisesRegex(UiPackageError, "UI package build failed"):
                build_ui_package(repository, self.root / "timeout.tgz")

    def test_build_uses_offline_install_without_lifecycle_scripts_or_host_secrets(self):
        from types import SimpleNamespace
        with (
            patch.dict(os.environ, {"PATH_SECRET": "PRIVATE"}),
            patch.object(ui_package, "_run_supervised") as execute,
        ):
            ui_package._run_build(SimpleNamespace(stage_fd=42))
        install = next(call for call in execute.call_args_list if "ci" in call.args[1])
        self.assertIn("--offline", install.args[1])
        self.assertIn("--ignore-scripts", install.args[1])
        for call in execute.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            environment = call.kwargs["environment"]
            self.assertNotIn("PATH_SECRET", environment)
            self.assertEqual(environment["NPM_CONFIG_OFFLINE"], "true")
            self.assertNotIn("PRIVATE", str(environment))


    def test_root_and_ancestor_swaps_do_not_change_the_packaged_tree(self):
        for kind in ("root", "ancestor"):
            with self.subTest(kind=kind):
                repository = self._repository(f"{kind}-ancestor/repository")
                evil = self._repository(f"{kind}-evil/repository")
                (evil / "packages" / "ui" / "dist" / "index.js").write_text(
                    "EVIL", encoding="utf-8"
                )
                output = self.root / f"{kind}.tgz"
                output.parent.mkdir(exist_ok=True)

                def swap(*_args, **_kwargs):
                    if kind == "root":
                        repository.rename(self.root / "moved-repository")
                        evil.rename(repository)
                    else:
                        (self.root / f"{kind}-ancestor").rename(self.root / "moved-ancestor")
                        (self.root / f"{kind}-evil").rename(self.root / f"{kind}-ancestor")

                def staged_swap(snapshot):
                    dist = snapshot.path / "packages" / "ui" / "dist"
                    dist.mkdir()
                    for name in PUBLIC_DIST_FILES:
                        (dist / name).write_text("export {};\n", encoding="utf-8")
                    swap()

                with patch("local_web_server.ui_package._run_build", side_effect=staged_swap):
                    artifact = build_ui_package(repository, output)
                with tarfile.open(artifact.path, mode="r:gz") as archive:
                    self.assertEqual(
                        archive.extractfile("package/dist/index.js").read(),
                        b"export {};\n",
                    )

    def test_live_package_and_dist_swaps_do_not_change_the_committed_snapshot(self):
        for kind in ("package", "dist"):
            with self.subTest(kind=kind):
                repository = self._repository(f"{kind}-repository")
                evil = self._repository(f"{kind}-evil")
                output = self.root / f"{kind}.tgz"
                output.parent.mkdir(exist_ok=True)

                def swap(*_args, **_kwargs):
                    if kind == "package":
                        original = repository / "packages" / "ui"
                        original.rename(repository / "packages" / "original-ui")
                        (evil / "packages" / "ui").rename(original)
                    else:
                        original = repository / "packages" / "ui" / "dist"
                        original.rename(repository / "packages" / "ui" / "original-dist")
                        (evil / "packages" / "ui" / "dist").rename(original)

                def staged_swap(snapshot):
                    dist = snapshot.path / "packages" / "ui" / "dist"
                    dist.mkdir()
                    for name in PUBLIC_DIST_FILES:
                        (dist / name).write_text("export {};\n", encoding="utf-8")
                    swap()

                with patch("local_web_server.ui_package._run_build", side_effect=staged_swap):
                    artifact = build_ui_package(repository, output)
                with tarfile.open(artifact.path, mode="r:gz") as archive:
                    self.assertEqual(
                        archive.extractfile("package/dist/index.js").read(),
                        b"export {};\n",
                    )

    def test_output_write_failures_preserve_a_verified_prior_artifact(self):
        repository = self._repository()
        output = self.root / "output.tgz"
        first, _ = self._build_fixture(repository, output)
        before = first.path.read_bytes()
        for boundary, target in (
            ("write", "local_web_server.ui_package._write_tarball"),
            ("chmod", "local_web_server.ui_package.os.fchmod"),
            ("replace", "local_web_server.ui_package.os.replace"),
        ):
            with self.subTest(boundary=boundary), patch(target, side_effect=OSError("injected")):
                with self.assertRaisesRegex(UiPackageError, "UI package output failed"):
                    self._build_fixture(repository, output)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertFalse(list(output.parent.glob(".local-web-ui-*")))

        original_unlink = os.unlink
        calls = 0
        output_parent = output.parent.stat()

        def transient_cleanup_failure(path, *, dir_fd=None):
            nonlocal calls
            is_output_parent = (
                dir_fd is not None and os.fstat(dir_fd).st_ino == output_parent.st_ino
            )
            if is_output_parent:
                calls += 1
            if is_output_parent and calls == 1:
                raise OSError("injected")
            return original_unlink(path, dir_fd=dir_fd)

        with patch("local_web_server.ui_package._write_tarball", side_effect=UiPackageError("UI package output failed")), patch(
            "local_web_server.ui_package.os.unlink", side_effect=transient_cleanup_failure
        ):
            with self.assertRaisesRegex(UiPackageError, "UI package output failed"):
                self._build_fixture(repository, output)
        self.assertEqual(output.read_bytes(), before)
        self.assertFalse(list(output.parent.glob(".local-web-ui-*")))

    def test_persistent_output_unlink_failure_moves_partial_file_out_of_the_artifact_parent(self):
        repository = self._repository()
        output = self.root / "persistent-cleanup.tgz"
        first, _ = self._build_fixture(repository, output)
        before = first.path.read_bytes()
        parent_identity = output.parent.stat()
        original_unlink = os.unlink
        descriptors_before = len(os.listdir("/dev/fd"))

        def persistent_parent_unlink(path, *, dir_fd=None):
            if dir_fd is not None and os.fstat(dir_fd).st_ino == parent_identity.st_ino:
                raise OSError("persistent injected output-parent unlink failure")
            return original_unlink(path, dir_fd=dir_fd)

        with patch("local_web_server.ui_package._write_tarball", side_effect=UiPackageError("UI package output failed")), patch(
            "local_web_server.ui_package.os.unlink", side_effect=persistent_parent_unlink
        ):
            with self.assertRaisesRegex(UiPackageError, "UI package output failed"):
                self._build_fixture(repository, output)

        self.assertEqual(output.read_bytes(), before)
        self.assertFalse(list(output.parent.glob(".local-web-ui-*")))
        self.assertEqual(len(os.listdir("/dev/fd")), descriptors_before)

    def test_rejects_dirty_or_uncommitted_build_inputs_before_staging(self):
        for relative, payload in (
            ("package-lock.json", "{}\n"),
            ("packages/ui/vite.config.ts", "throw new Error('dirty');\n"),
            ("packages/ui/src/uncommitted.ts", "export const dirty = true;\n"),
        ):
            with self.subTest(relative=relative):
                repository = self._repository(relative.replace("/", "-"))
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")

                with self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
                    self._build_fixture(repository, self.root / f"dirty-{target.name}.tgz")

                self.assertNotIn(str(repository), str(caught.exception))

    def test_snapshot_uses_committed_bytes_during_a_complete_live_config_swap(self):
        repository = self._repository("committed-snapshot")
        config = repository / "packages" / "ui" / "vite.config.ts"
        original = config.read_bytes()
        output = self.root / "committed-snapshot.tgz"
        real_verify = ui_package._verify_worktree_inputs

        def verify_then_swap(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            config.write_text(
                "import { writeFileSync } from 'node:fs'; writeFileSync('CONFIG_EXECUTED', 'x');\n",
                encoding="utf-8",
            )
            return result

        def fake_build(snapshot):
            try:
                staged = snapshot.path / "packages" / "ui" / "vite.config.ts"
                self.assertEqual(staged.read_bytes(), original)
                self.assertNotIn(b"writeFileSync", staged.read_bytes())
                dist = snapshot.path / "packages" / "ui" / "dist"
                dist.mkdir()
                for name in PUBLIC_DIST_FILES:
                    (dist / name).write_text("export {};\n", encoding="utf-8")
            finally:
                config.write_bytes(original)

        with patch(
            "local_web_server.ui_package._verify_worktree_inputs",
            side_effect=verify_then_swap,
        ), patch("local_web_server.ui_package._run_build", side_effect=fake_build):
            artifact = build_ui_package(repository, output)

        self.assertTrue(artifact.path.exists())
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((repository / "CONFIG_EXECUTED").exists())

    def test_stage_replacement_after_cleanup_entry_check_is_preserved(self):
        repository = self._repository("stage-cleanup-race")
        output = self.root / "stage-cleanup-race.tgz"
        real_remove = ui_package._remove_tree_fd
        replacement_marker = b"replacement survives"
        moved_stage: Path | None = None
        replacement_stage: Path | None = None
        active_stage: Path | None = None
        remove_calls = 0

        def fake_build(snapshot):
            nonlocal active_stage
            active_stage = snapshot.path
            dist = snapshot.path / "packages" / "ui" / "dist"
            dist.mkdir()
            for name in PUBLIC_DIST_FILES:
                (dist / name).write_text("export {};\n", encoding="utf-8")

        def swap_after_check(directory_fd):
            nonlocal moved_stage, replacement_stage, remove_calls
            remove_calls += 1
            if remove_calls == 1:
                self.assertIsNotNone(active_stage)
                stage_path = active_stage
                moved_stage = stage_path.with_name(f"{stage_path.name}-moved")
                stage_path.rename(moved_stage)
                stage_path.mkdir()
                replacement_stage = stage_path
                (stage_path / "replacement-marker").write_bytes(replacement_marker)
            return real_remove(directory_fd)

        with patch("local_web_server.ui_package._run_build", side_effect=fake_build), patch(
            "local_web_server.ui_package._remove_tree_fd", side_effect=swap_after_check
        ), self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
            build_ui_package(repository, output)

        self.assertFalse(output.exists())
        self.assertIsNotNone(replacement_stage)
        marker = replacement_stage / "replacement-marker"
        self.assertEqual(marker.read_bytes(), replacement_marker)
        marker.unlink()
        replacement_stage.rmdir()
        if moved_stage is not None and moved_stage.exists():
            moved_stage.rmdir()

    def test_zero_exit_sigterm_ignoring_descendant_is_killed_before_return(self):
        stage = Path(tempfile.mkdtemp(prefix="ui-process-group-"))
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        marker = self.root / "late-descendant"
        ready = self.root / "descendant-ready"
        child = self.root / "ignoring-child.py"
        child.write_text(
            "import signal, time\n"
            f"from pathlib import Path\nPath({str(ready)!r}).touch()\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"time.sleep(1)\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        script = self.root / "group-leader.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"'{os.sys.executable}' '{child}' &\n"
            f"while [ ! -e '{ready}' ]; do sleep 0.01; done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        try:
            ui_package._run_supervised(stage_fd, [str(script)], timeout=2)
            time.sleep(1.2)
            self.assertFalse(marker.exists())
        finally:
            os.close(stage_fd)
            stage.rmdir()

    def test_helper_closes_staging_descriptor_before_exec(self):
        stage = Path(tempfile.mkdtemp(prefix="ui-helper-fd-"))
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        result = self.root / "helper-fd-result"
        probe = self.root / "probe-fd.py"
        probe.write_text(
            "import os, sys\n"
            "try:\n"
            "    os.fstat(int(sys.argv[1]))\n"
            "except OSError:\n"
            "    open(sys.argv[2], 'w').write('closed')\n"
            "else:\n"
            "    open(sys.argv[2], 'w').write('open')\n",
            encoding="utf-8",
        )
        try:
            ui_package._run_supervised(
                stage_fd,
                [os.fspath(Path(os.sys.executable)), os.fspath(probe), str(stage_fd), os.fspath(result)],
                timeout=2,
            )
            self.assertEqual(result.read_text(encoding="utf-8"), "closed")
        finally:
            os.close(stage_fd)
            stage.rmdir()

    def test_primary_build_error_is_not_masked_by_stage_cleanup_failure(self):
        repository = self._repository("composed-cleanup")
        active_stage: Path | None = None

        def fail_build(snapshot):
            nonlocal active_stage
            active_stage = snapshot.path
            raise UiPackageError("UI package build failed")

        try:
            with patch(
                "local_web_server.ui_package._run_build",
                side_effect=fail_build,
            ), patch(
                "local_web_server.ui_package._remove_tree_fd",
                side_effect=OSError(f"cleanup failed {_PRIVATE_TEST_PATH}"),
            ):
                with self.assertRaisesRegex(UiPackageError, "UI package build failed") as caught:
                    build_ui_package(repository, self.root / "composed-cleanup.tgz")

            self.assertIsInstance(caught.exception.__cause__, UiPackageError)
            self.assertEqual(str(caught.exception.__cause__), "UI package cleanup failed")
            self.assertNotIn(_PRIVATE_TEST_PATH, str(caught.exception))
        finally:
            if active_stage is not None and active_stage.exists():
                shutil.rmtree(active_stage)

    def test_impossible_partial_cleanup_reports_failure_and_preserves_prior_artifact(self):
        repository = self._repository("impossible-output-cleanup")
        output = self.root / "impossible-output-cleanup.tgz"
        first, _ = self._build_fixture(repository, output)
        before = first.path.read_bytes()
        output_parent = output.parent.stat()
        original_unlink = os.unlink
        original_remove = os.remove
        original_replace = os.replace

        def fail_output_unlink(path, *, dir_fd=None):
            if dir_fd is not None and os.fstat(dir_fd).st_ino == output_parent.st_ino:
                raise OSError(f"unlink failed {_PRIVATE_TEST_PATH}")
            return original_unlink(path, dir_fd=dir_fd)

        def fail_output_remove(path, *, dir_fd=None):
            if dir_fd is not None and os.fstat(dir_fd).st_ino == output_parent.st_ino:
                raise OSError(f"remove failed {_PRIVATE_TEST_PATH}")
            return original_remove(path, dir_fd=dir_fd)

        def fail_output_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if src_dir_fd is not None and os.fstat(src_dir_fd).st_ino == output_parent.st_ino:
                raise OSError(f"rename failed {_PRIVATE_TEST_PATH}")
            return original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with patch(
            "local_web_server.ui_package._write_tarball",
            side_effect=UiPackageError("UI package output failed"),
        ), patch(
            "local_web_server.ui_package._unlink_output_temporary",
            side_effect=OSError(f"wrapper failed {_PRIVATE_TEST_PATH}"),
        ), patch(
            "local_web_server.ui_package.os.unlink",
            side_effect=fail_output_unlink,
        ), patch(
            "local_web_server.ui_package.os.remove",
            side_effect=fail_output_remove,
        ), patch(
            "local_web_server.ui_package.os.replace",
            side_effect=fail_output_replace,
        ):
            with self.assertRaisesRegex(UiPackageError, "UI package output failed") as caught:
                self._build_fixture(repository, output)

        self.assertEqual(output.read_bytes(), before)
        self.assertIsInstance(caught.exception.__cause__, UiPackageError)
        self.assertEqual(str(caught.exception.__cause__), "UI package cleanup failed")
        for partial in output.parent.glob(".local-web-ui-*"):
            partial.unlink()

    def test_stage_parent_pin_failure_does_not_leave_a_private_stage(self):
        repository = self._repository("stage-parent-failure")
        temporary_parent = self.root / "stage-parent"
        temporary_parent.mkdir()
        real_pin = ui_package._pin_directory

        def fail_only_for_temporary_parent(path):
            if Path(path).resolve() == temporary_parent:
                raise UiPackageError("UI package validation failed")
            return real_pin(path)

        with patch("tempfile.tempdir", os.fspath(temporary_parent)), patch(
            "local_web_server.ui_package._pin_directory",
            side_effect=fail_only_for_temporary_parent,
        ), self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
            self._build_fixture(repository, self.root / "stage-parent-failure.tgz")

        self.assertEqual(list(temporary_parent.iterdir()), [])

    def test_path_bearing_stage_write_failure_is_bounded_and_cleans_the_stage(self):
        repository = self._repository("stage-write-failure")
        temporary_parent = self.root / "stage-write"
        temporary_parent.mkdir()

        with patch("tempfile.tempdir", os.fspath(temporary_parent)), patch(
            "local_web_server.ui_package._write_stage_file",
            side_effect=OSError(f"write failed {_PRIVATE_TEST_PATH}"),
        ), self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
            self._build_fixture(repository, self.root / "stage-write-failure.tgz")

        self.assertNotIn(_PRIVATE_TEST_PATH, str(caught.exception))
        self.assertEqual(list(temporary_parent.iterdir()), [])

    def test_git_replace_ref_cannot_change_the_recorded_head_authority(self):
        repository = self._repository("replace-ref")
        config = repository / "packages" / "ui" / "vite.config.ts"
        good_config = config.read_bytes()
        good_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        evil_config = b"throw new Error('replacement config executed');\n"
        config.write_bytes(evil_config)
        subprocess.run(["git", "add", "packages/ui/vite.config.ts"], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Task Six Tests",
                "-c",
                "user.email=task-six@example.invalid",
                "commit",
                "-qm",
                "evil replacement",
            ],
            cwd=repository,
            check=True,
        )
        evil_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        subprocess.run(["git", "reset", "--hard", "-q", good_commit], cwd=repository, check=True)
        subprocess.run(["git", "replace", good_commit, evil_commit], cwd=repository, check=True)
        config.write_bytes(evil_config)

        with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
            self._build_fixture(repository, self.root / "replace-ref.tgz")

        self.assertFalse((self.root / "replace-ref.tgz").exists())
        self.assertNotEqual(config.read_bytes(), good_config)
        config.write_bytes(good_config)
        pinned = ui_package._pin_directory(repository)
        authority = ui_package._load_git_authority(pinned)
        try:
            expected_tree = subprocess.check_output(
                ["git", "--no-replace-objects", "rev-parse", f"{good_commit}^{{tree}}"],
                cwd=repository,
                text=True,
            ).strip()
            self.assertEqual(authority.commit, good_commit)
            self.assertEqual(authority.tree, expected_tree)
            self.assertEqual(authority.sources["packages/ui/vite.config.ts"], good_config)
        finally:
            authority.close()
            pinned.close()

        def inspect_committed_stage(snapshot):
            self.assertEqual(
                (snapshot.path / "packages" / "ui" / "vite.config.ts").read_bytes(),
                good_config,
            )
            dist = snapshot.path / "packages" / "ui" / "dist"
            dist.mkdir()
            for name in PUBLIC_DIST_FILES:
                (dist / name).write_text("export {};\n", encoding="utf-8")

        with patch(
            "local_web_server.ui_package._run_build", side_effect=inspect_committed_stage
        ):
            artifact = build_ui_package(repository, self.root / "replace-ref-clean.tgz")
        self.assertTrue(artifact.path.is_file())

    def test_authority_validation_remains_primary_when_git_cleanup_also_fails(self):
        repository = self._repository("authority-error-composition")
        descriptors_before = len(os.listdir("/dev/fd"))

        with patch(
            "local_web_server.ui_package._run_captured",
            side_effect=UiPackageError("UI package validation failed"),
        ), patch(
            "local_web_server.ui_package._before_descriptor_close",
            side_effect=OSError(f"cleanup failed {_PRIVATE_TEST_PATH}"),
        ), self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
            build_ui_package(repository, self.root / "authority-error-composition.tgz")

        self.assertIsInstance(caught.exception.__cause__, UiPackageError)
        self.assertEqual(str(caught.exception.__cause__), "UI package cleanup failed")
        self.assertNotIn(_PRIVATE_TEST_PATH, str(caught.exception))
        self.assertEqual(len(os.listdir("/dev/fd")) - descriptors_before, 0)

    def test_staged_validation_remains_primary_when_tree_cleanup_also_fails(self):
        repository = self._repository("staged-error-composition")
        descriptors_before = len(os.listdir("/dev/fd"))
        cleanup_armed = False
        cleanup_failures_remaining = 3

        def reject_staged_package(_tree):
            nonlocal cleanup_armed
            cleanup_armed = True
            raise UiPackageError("UI package validation failed")

        def fail_cleanup_close():
            nonlocal cleanup_failures_remaining
            if cleanup_armed and cleanup_failures_remaining:
                cleanup_failures_remaining -= 1
                raise OSError(f"cleanup failed {_PRIVATE_TEST_PATH}")

        with patch(
            "local_web_server.ui_package._snapshot_package",
            side_effect=reject_staged_package,
        ), patch(
            "local_web_server.ui_package._before_descriptor_close",
            side_effect=fail_cleanup_close,
        ), self.assertRaisesRegex(UiPackageError, "UI package validation failed") as caught:
            self._build_fixture(repository, self.root / "staged-error-composition.tgz")

        self.assertIsInstance(caught.exception.__cause__, UiPackageError)
        self.assertEqual(str(caught.exception.__cause__), "UI package cleanup failed")
        self.assertNotIn(_PRIVATE_TEST_PATH, str(caught.exception))
        self.assertEqual(len(os.listdir("/dev/fd")) - descriptors_before, 0)

    @acceptance
    def test_clean_clone_pack_builds_the_canonical_public_release(self):
        source = Path(__file__).parents[1]
        clone = self.root / "fresh-clone"
        subprocess.run(
            ["git", "clone", "-q", os.fspath(source), os.fspath(clone)],
            check=True,
        )
        self.assertFalse((clone / "packages" / "ui" / "dist").exists())
        self.assertFalse((clone / "node_modules").exists())

        result = subprocess.run(
            ["npm", "run", "pack:ui"], cwd=clone,
            env={**os.environ, "NPM_CONFIG_USERCONFIG": os.devnull,
                 "NPM_CONFIG_OFFLINE": "true", "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                 "NPM_CONFIG_REGISTRY": "https://registry.invalid"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        artifact = clone / "artifacts/local-web-ui.tgz"
        with tarfile.open(artifact, "r:gz") as archive:
            files = {
                member.name for member in archive.getmembers() if member.isfile()
            }
            self.assertEqual(
                files,
                {
                    "package/package.json",
                    *(f"package/dist/{name}" for name in PUBLIC_DIST_FILES),
                },
            )
            self.assertFalse(
                any(
                    re.search(
                        r"(?:\.map$|secret|credential|token|\.env|private)",
                        name,
                        re.I,
                    )
                    for name in files
                )
            )
            metadata = archive.extractfile("package/package.json").read()
            version, canonical = ui_package._canonical_metadata(metadata)
            self.assertEqual(version, CURRENT_UI_PACKAGE_VERSION)
            self.assertEqual(metadata, canonical)

    def test_terminal_capture_preserves_root_child_and_file_replacements(self):
        real_capture = ui_package._native_rename_exclusive
        for kind in ("root", "child", "file"):
            with self.subTest(kind=kind):
                parent = self.root / f"terminal-{kind}"
                parent.mkdir()
                target = parent / "target"
                moved = parent / "moved-original"
                is_directory = kind != "file"
                if is_directory:
                    target.mkdir()
                else:
                    target.write_text("original", encoding="utf-8")
                expected = target.stat()
                parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                swapped = False

                def swap_at_capture(parent_descriptor, old_name, quarantine_name):
                    nonlocal swapped
                    if not swapped:
                        target.rename(moved)
                        if is_directory:
                            target.mkdir()
                            (target / "replacement-marker").write_text(
                                "replacement", encoding="utf-8"
                            )
                        else:
                            target.write_text("replacement", encoding="utf-8")
                        swapped = True
                    return real_capture(parent_descriptor, old_name, quarantine_name)

                try:
                    with patch(
                        "local_web_server.ui_package._native_rename_exclusive",
                        side_effect=swap_at_capture,
                    ), self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
                        ui_package._terminal_remove_at(
                            parent_fd, "target", expected, is_directory=is_directory
                        )
                    self.assertTrue(target.exists())
                    if is_directory:
                        self.assertEqual(
                            (target / "replacement-marker").read_text(encoding="utf-8"),
                            "replacement",
                        )
                    else:
                        self.assertEqual(target.read_text(encoding="utf-8"), "replacement")
                    self.assertTrue(moved.exists())
                finally:
                    os.close(parent_fd)

    def test_close_does_not_touch_a_concurrently_reused_descriptor_number(self):
        real_os_close = os.close
        real_native_close = ui_package._native_close
        owned = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        released = threading.Event()
        sentinel_ready = threading.Event()
        sentinel: list[int] = []

        def acquire_reused_sentinel():
            self.assertTrue(released.wait(2))
            sentinel.append(os.open(self.root, os.O_RDONLY | os.O_DIRECTORY))
            sentinel_ready.set()

        def await_reuse():
            released.set()
            self.assertTrue(sentinel_ready.wait(2))

        def ambiguous_python_close(descriptor):
            real_os_close(descriptor)
            await_reuse()
            raise OSError("close reported failure after releasing ownership")

        def instrumented_native_close(descriptor):
            real_native_close(descriptor)
            await_reuse()

        worker = threading.Thread(target=acquire_reused_sentinel)
        worker.start()
        try:
            with patch(
                "local_web_server.ui_package.os.close",
                side_effect=ambiguous_python_close,
            ), patch(
                "local_web_server.ui_package._native_close",
                side_effect=instrumented_native_close,
            ):
                ui_package._close_descriptor(owned)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(sentinel, [owned])
            os.fstat(sentinel[0])
        finally:
            released.set()
            worker.join(2)
            for descriptor in sentinel or [owned]:
                try:
                    real_os_close(descriptor)
                except OSError:
                    pass

    def test_repeated_preclose_failures_close_descriptors_without_fd_growth(self):
        descriptors_before = len(os.listdir("/dev/fd"))

        with patch(
            "local_web_server.ui_package._before_descriptor_close",
            side_effect=OSError("injected persistent close failure"),
        ):
            for _ in range(100):
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                with self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
                    ui_package._close_descriptor(descriptor)

        self.assertEqual(len(os.listdir("/dev/fd")), descriptors_before)

    def test_native_close_error_relinquishes_the_owned_number_without_retry(self):
        real_native_close = ui_package._native_close
        owned = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        pinned = ui_package._PinnedDirectory(self.root, [owned])
        close_attempts: list[int] = []
        sentinel: list[int] = []

        def close_then_report_error(descriptor):
            close_attempts.append(descriptor)
            real_native_close(descriptor)
            sentinel.append(os.open(self.root, os.O_RDONLY | os.O_DIRECTORY))
            raise UiPackageError("UI package cleanup failed")

        try:
            with patch(
                "local_web_server.ui_package._native_close",
                side_effect=close_then_report_error,
            ):
                with self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
                    pinned.close()
                pinned.close()

            self.assertEqual(close_attempts, [owned])
            self.assertEqual(sentinel, [owned])
            os.fstat(sentinel[0])
        finally:
            for descriptor in sentinel or [owned]:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_preclose_cleanup_failure_is_composed_behind_primary_build_error(self):
        repository = self._repository("preclose-composition")
        build_failed = False
        active_stage: Path | None = None

        def fail_build(snapshot):
            nonlocal active_stage, build_failed
            active_stage = snapshot.path
            build_failed = True
            raise UiPackageError("UI package build failed")

        def fail_cleanup_close():
            if build_failed:
                raise OSError(f"injected close cleanup failure {_PRIVATE_TEST_PATH}")

        try:
            with patch(
                "local_web_server.ui_package._run_build",
                side_effect=fail_build,
            ), patch(
                "local_web_server.ui_package._before_descriptor_close",
                side_effect=fail_cleanup_close,
            ), self.assertRaisesRegex(UiPackageError, "UI package build failed") as caught:
                build_ui_package(repository, self.root / "preclose-composition.tgz")

            self.assertIsInstance(caught.exception.__cause__, UiPackageError)
            self.assertEqual(str(caught.exception.__cause__), "UI package cleanup failed")
            self.assertNotIn(_PRIVATE_TEST_PATH, str(caught.exception))
        finally:
            if active_stage is not None and active_stage.exists():
                shutil.rmtree(active_stage)

    def test_output_preclose_failure_closes_and_removes_the_partial(self):
        repository = self._repository("output-preclose")
        output = self.root / "output-preclose.tgz"
        first, _ = self._build_fixture(repository, output)
        before = first.path.read_bytes()
        real_write_tarball = ui_package._write_tarball
        close_failure_armed = False

        def write_then_arm(entries, descriptor):
            nonlocal close_failure_armed
            digest = real_write_tarball(entries, descriptor)
            close_failure_armed = True
            return digest

        def fail_output_close():
            nonlocal close_failure_armed
            if close_failure_armed:
                close_failure_armed = False
                raise OSError("injected pre-close failure")

        with patch(
            "local_web_server.ui_package._write_tarball",
            side_effect=write_then_arm,
        ), patch(
            "local_web_server.ui_package._before_descriptor_close",
            side_effect=fail_output_close,
        ), self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
            self._build_fixture(repository, output)

        self.assertEqual(output.read_bytes(), before)
        self.assertFalse(list(output.parent.glob(".local-web-ui-*")))

    def test_missing_repository_has_a_bounded_error_and_does_not_close_unrelated_fds(self):
        sentinel = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(UiPackageError, "UI package validation failed"):
                build_ui_package(self.root / "missing-repository", self.root / "missing.tgz")
            os.fstat(sentinel)
        finally:
            os.close(sentinel)

    def test_live_node_modules_is_never_copied_into_the_committed_snapshot(self):
        repository = self._repository("no-live-toolchain")
        (repository / "node_modules" / "attacker-tool.js").write_text(
            "throw new Error('executed live tool');\n", encoding="utf-8"
        )

        def inspect_snapshot(snapshot):
            self.assertFalse((snapshot.path / "node_modules").exists())
            dist = snapshot.path / "packages" / "ui" / "dist"
            dist.mkdir()
            for name in PUBLIC_DIST_FILES:
                (dist / name).write_text("export {};\n", encoding="utf-8")

        with patch("local_web_server.ui_package._run_build", side_effect=inspect_snapshot):
            artifact = build_ui_package(repository, self.root / "no-live-toolchain.tgz")

        self.assertTrue(artifact.path.exists())


    def test_recursive_stage_child_swap_preserves_the_replacement(self):
        repository = self._repository("recursive-stage-race")
        output = self.root / "recursive-stage-race.tgz"
        real_open = ui_package._open_directory_at
        real_remove = ui_package._remove_tree_fd
        active_stage: Path | None = None
        cleanup_active = False
        swapped = False

        def fake_build(snapshot):
            nonlocal active_stage
            active_stage = snapshot.path
            dist = snapshot.path / "packages" / "ui" / "dist"
            dist.mkdir()
            for name in PUBLIC_DIST_FILES:
                (dist / name).write_text("export {};\n", encoding="utf-8")

        def begin_cleanup(directory_fd):
            nonlocal cleanup_active
            cleanup_active = True
            return real_remove(directory_fd)

        def swap_child(parent_fd, name):
            nonlocal swapped
            if cleanup_active and not swapped and name == "packages":
                self.assertIsNotNone(active_stage)
                packages = active_stage / "packages"
                packages.rename(active_stage / "packages-moved")
                packages.mkdir()
                (packages / "replacement-marker").write_text("survives", encoding="utf-8")
                swapped = True
            return real_open(parent_fd, name)

        try:
            with patch("local_web_server.ui_package._run_build", side_effect=fake_build), patch(
                "local_web_server.ui_package._remove_tree_fd", side_effect=begin_cleanup
            ), patch(
                "local_web_server.ui_package._open_directory_at", side_effect=swap_child
            ), self.assertRaisesRegex(UiPackageError, "UI package cleanup failed"):
                build_ui_package(repository, output)

            self.assertTrue(swapped)
            self.assertEqual(
                (active_stage / "packages" / "replacement-marker").read_text(encoding="utf-8"),
                "survives",
            )
            self.assertTrue((active_stage / "packages-moved" / "ui").is_dir())
            self.assertFalse(output.exists())
        finally:
            if active_stage is not None and active_stage.exists():
                shutil.rmtree(active_stage)


if __name__ == "__main__":
    unittest.main()
