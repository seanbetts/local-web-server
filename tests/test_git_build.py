import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.git_build import (
    BuildFailed,
    BuildOutputInvalid,
    Builder,
    GitRepository,
    _validated_output,
)
from local_web_server.models import AppManifest, BuildSpec, Command, HostApp, ReleaseEntry
from local_web_server.runtime import RuntimeLayout, RuntimeLayoutError
from tests.helpers import init_git_repo


class GitBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.manifest_payload = {
            "schemaVersion": 1,
            "id": "example",
            "title": "Committed Example",
            "route": "/example",
            "kind": "static",
            "build": {
                "commands": [["python3", "-c", "pass"]],
                "output": "dist",
                "environment": ["COMMITTED_VALUE"],
            },
            "healthPath": "/example/committed-health",
        }
        self.provenance_payload = {
            "schemaVersion": 1,
            "templateVersion": 1,
            "platformContractVersion": 1,
            "ui": {"version": "0.4.0", "sha256": "a" * 64},
            "capabilities": [],
            "domainPaletteTokens": [],
            "managedFiles": [],
        }
        self.commit = init_git_repo(
            self.repository,
            {
                "source.txt": "committed",
                "local-web.json": json.dumps(self.manifest_payload),
                ".local-web-platform.json": json.dumps(self.provenance_payload),
            },
        )
        (self.repository / "source.txt").write_text("dirty", encoding="utf-8")
        self.host = HostApp(
            id="example",
            repository=self.repository,
            auto_deploy=True,
            environment_file=None,
            environment=(),
            port=None,
            start_command=None,
        )
        self.layout = RuntimeLayout(self.root / "runtime", "example")
        self.log = (self.root / "build.log").open("w", encoding="utf-8")

    def tearDown(self):
        self.log.close()
        self.temp.cleanup()

    def manifest(self, command: Command, output: Path = Path("dist")) -> AppManifest:
        return AppManifest(
            schema_version=1,
            id="example",
            title="Example",
            route="/example",
            kind="static",
            build=BuildSpec(commands=(command,), output=output, environment=()),
            health_path="/example/",
            service=None,
        )

    def worktrees(self) -> list[Path]:
        output = subprocess.run(
            ["git", "-C", str(self.repository), "worktree", "list", "--porcelain"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        return [Path(line.removeprefix("worktree ")) for line in output.splitlines() if line.startswith("worktree ")]

    def test_build_uses_the_committed_main_revision_and_removes_its_worktree(self):
        manifest = self.manifest(Command((
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('dist').mkdir(); "
            "Path('dist/result.txt').write_text(Path('source.txt').read_text())",
        )))

        release = Builder().build(
            self.host, manifest, self.commit, self.layout, {"PATH": "/usr/bin:/bin"}, self.log
        )

        self.assertEqual(release, self.layout.releases / self.commit)
        self.assertEqual((release / "result.txt").read_text(encoding="utf-8"), "committed")
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_build_composes_declarative_release_before_immutable_publication(self):
        manifest = self.manifest(
            Command((
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "Path('dist').mkdir(); "
                "Path('dist/index.html').write_text('<main>service</main>')",
            )),
            Path("release"),
        )
        manifest = AppManifest(
            **{
                **manifest.__dict__,
                "build": BuildSpec(
                    commands=manifest.build.commands,
                    output=Path("release"),
                    environment=(),
                    release_entries=(
                        ReleaseEntry(Path("dist"), Path("public")),
                        ReleaseEntry(Path("source.txt"), Path("server/source.txt")),
                    ),
                ),
            }
        )

        release = Builder().build(
            self.host, manifest, self.commit, self.layout, {"PATH": "/usr/bin:/bin"}, self.log
        )

        self.assertEqual((release / "public/index.html").read_text(), "<main>service</main>")
        self.assertEqual((release / "server/source.txt").read_text(), "committed")

    def test_build_ignores_hook_local_git_index_environment(self):
        manifest = self.manifest(Command((
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('dist').mkdir(); "
            "Path('dist/result.txt').write_text(Path('source.txt').read_text())",
        )))

        with patch.dict(os.environ, {"GIT_INDEX_FILE": ".git/index"}):
            release = Builder().build(
                self.host, manifest, self.commit, self.layout,
                {"PATH": "/usr/bin:/bin"}, self.log,
            )

        self.assertEqual((release / "result.txt").read_text(encoding="utf-8"), "committed")
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_nonzero_command_raises_with_its_argv_and_exit_code(self):
        command = Command((sys.executable, "-c", "raise SystemExit(23)"))

        with self.assertRaises(BuildFailed) as raised:
            Builder().build(
                self.host, self.manifest(command), self.commit, self.layout, {}, self.log
            )

        self.assertEqual(raised.exception.argv, command.argv)
        self.assertEqual(raised.exception.returncode, 23)
        self.assertIn("23", str(raised.exception))
        self.assertFalse((self.layout.releases / self.commit).exists())
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_symlinked_output_escaping_the_worktree_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        command = Command((
            sys.executable,
            "-c",
            f"from pathlib import Path; Path('dist').symlink_to({str(outside)!r}, target_is_directory=True)",
        ))

        with self.assertRaises(BuildOutputInvalid):
            Builder().build(
                self.host, self.manifest(command), self.commit, self.layout, {}, self.log
            )

        self.assertFalse((self.layout.releases / self.commit).exists())
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_git_metadata_directory_cannot_be_the_declared_output_root(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist/.git').mkdir(parents=True)",
        ))

        with self.assertRaises(BuildOutputInvalid):
            Builder().build(
                self.host, self.manifest(command, Path("dist/.git")), self.commit,
                self.layout, {}, self.log,
            )

        self.assertFalse((self.layout.releases / self.commit).exists())
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_environment_directory_cannot_be_the_declared_output_root(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist/.env.local').mkdir(parents=True)",
        ))

        with self.assertRaises(BuildOutputInvalid):
            Builder().build(
                self.host, self.manifest(command, Path("dist/.env.local")), self.commit,
                self.layout, {}, self.log,
            )

        self.assertFalse((self.layout.releases / self.commit).exists())
        self.assertEqual(self.worktrees(), [self.repository.resolve()])

    def test_repository_reports_main_commit_and_current_branch(self):
        repository = GitRepository(self.repository)

        self.assertEqual(repository.main_commit(), self.commit)
        self.assertEqual(repository.current_branch(), "main")

    def test_repository_loads_manifest_from_the_exact_commit_not_the_working_tree(self):
        dirty = dict(self.manifest_payload)
        dirty["title"] = "Dirty Example"
        dirty["healthPath"] = "/example/dirty-health"
        (self.repository / "local-web.json").write_text(json.dumps(dirty), encoding="utf-8")

        manifest = GitRepository(self.repository).manifest_at(self.commit)

        self.assertEqual(manifest.title, "Committed Example")
        self.assertEqual(manifest.health_path, "/example/committed-health")
        self.assertEqual(manifest.build.environment, ("COMMITTED_VALUE",))

    def test_repository_loads_provenance_from_the_exact_commit(self):
        dirty = dict(self.provenance_payload)
        dirty["templateVersion"] = 2
        (self.repository / ".local-web-platform.json").write_text(
            json.dumps(dirty), encoding="utf-8"
        )

        provenance = GitRepository(self.repository).provenance_at(self.commit)

        self.assertEqual(provenance.template_version, 1)
        self.assertEqual(provenance.ui.version, "0.4.0")

    def test_builder_refuses_symlinked_releases_without_touching_external_staging(self):
        external = self.root / "external-releases"
        external.mkdir()
        self.layout.app_root.mkdir(parents=True)
        self.layout.releases.symlink_to(external, target_is_directory=True)
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist').mkdir()",
        ))

        with self.assertRaisesRegex(ValueError, "runtime|release|symlink"):
            Builder().build(
                self.host, self.manifest(command), self.commit, self.layout, {}, self.log
            )

        self.assertEqual(list(external.iterdir()), [])

    def test_build_output_symlink_loop_is_a_build_domain_error(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist').symlink_to('dist', target_is_directory=True)",
        ))

        with self.assertRaises(BuildOutputInvalid):
            Builder().build(
                self.host, self.manifest(command), self.commit, self.layout, {}, self.log
            )

    def test_forced_cross_filesystem_publication_copies_nested_release(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('dist/nested').mkdir(parents=True); "
            "Path('dist/nested/result.txt').write_text('published'); "
            "Path('dist/run.sh').write_text('#!/bin/sh\\n'); "
            "Path('dist/run.sh').chmod(0o755)",
        ))
        real_rename = os.rename

        def force_cross_filesystem(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            return real_rename(source, destination, *args, **kwargs)

        with patch("local_web_server.git_build.os.rename", side_effect=force_cross_filesystem):
            release = Builder().build(
                self.host, self.manifest(command), self.commit, self.layout, {}, self.log
            )

        self.assertEqual((release / "nested" / "result.txt").read_text(), "published")
        self.assertTrue((release / "run.sh").stat().st_mode & 0o100)
        self.assertEqual(
            [path.name for path in self.layout.releases.iterdir()],
            [self.commit],
        )

    def test_publication_failure_preserves_primary_and_cleans_swapped_directory(self):
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('dist/nested').mkdir(parents=True); "
            "Path('dist/nested/result.txt').write_text('built')",
        ))
        real_rename = os.rename
        publication_error = OSError(errno.EIO, "forced publication failure")

        def fail_publication(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            if source == "release" and destination == self.commit:
                staging = next(
                    path for path in self.layout.releases.iterdir()
                    if path.name.startswith(".staging-")
                )
                nested = staging / "release" / "nested"
                nested.rename(staging / "release" / "original-nested")
                nested.symlink_to(external, target_is_directory=True)
                raise publication_error
            return real_rename(source, destination, *args, **kwargs)

        with patch("local_web_server.git_build.os.rename", side_effect=fail_publication):
            with self.assertRaises(OSError) as raised:
                Builder().build(
                    self.host, self.manifest(command), self.commit, self.layout, {}, self.log
                )

        self.assertIs(raised.exception, publication_error)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.layout.releases.iterdir()), [])

    def test_cleanup_failure_is_diagnostic_context_not_the_raised_error(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist').mkdir(); Path('dist/result').write_text('built')",
        ))
        real_rename = os.rename
        real_rmdir = os.rmdir
        publication_error = OSError(errno.EIO, "forced publication failure")

        def fail_publication(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            if source == "release" and destination == self.commit:
                raise publication_error
            return real_rename(source, destination, *args, **kwargs)

        def fail_staging_removal(path, *args, **kwargs):
            if str(path).startswith(".staging-") and kwargs.get("dir_fd") is not None:
                raise PermissionError(errno.EACCES, "forced cleanup failure")
            return real_rmdir(path, *args, **kwargs)

        with (
            patch("local_web_server.git_build.os.rename", side_effect=fail_publication),
            patch("local_web_server.git_build.os.rmdir", side_effect=fail_staging_removal),
        ):
            with self.assertRaises(OSError) as raised:
                Builder().build(
                    self.host, self.manifest(command), self.commit, self.layout, {}, self.log
                )

        self.assertIs(raised.exception, publication_error)
        self.assertTrue(
            any("staging cleanup failed" in note for note in raised.exception.__notes__)
        )

    def test_cross_filesystem_copy_rejects_directory_swapped_to_external_symlink(self):
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist/nested').mkdir(parents=True)",
        ))
        real_rename = os.rename

        def force_cross_filesystem(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            return real_rename(source, destination, *args, **kwargs)

        def validate_then_swap(worktree, output):
            validated = _validated_output(worktree, output)
            nested = validated / "nested"
            nested.rmdir()
            nested.symlink_to(external, target_is_directory=True)
            return validated

        with (
            patch("local_web_server.git_build.os.rename", side_effect=force_cross_filesystem),
            patch("local_web_server.git_build._validated_output", side_effect=validate_then_swap),
        ):
            with self.assertRaises(BuildOutputInvalid):
                Builder().build(
                    self.host, self.manifest(command), self.commit, self.layout, {}, self.log
                )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.layout.releases.iterdir()), [])

    def test_cross_filesystem_copy_rejects_special_files_and_cleans_staging(self):
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; import os; Path('dist').mkdir(); os.mkfifo('dist/pipe')",
        ))
        real_rename = os.rename

        def force_cross_filesystem(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            return real_rename(source, destination, *args, **kwargs)

        with patch("local_web_server.git_build.os.rename", side_effect=force_cross_filesystem):
            with self.assertRaises(BuildOutputInvalid):
                Builder().build(
                    self.host, self.manifest(command), self.commit, self.layout, {}, self.log
                )

        self.assertEqual(list(self.layout.releases.iterdir()), [])

    def test_staging_directory_swap_cannot_redirect_copy_or_cleanup(self):
        moved_staging = self.root / "moved-staging"
        sentinel = moved_staging / "keep.txt"
        command = Command((
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist').mkdir(); Path('dist/result').write_text('built')",
        ))
        real_rename = os.rename

        def move_staging_then_force_cross_filesystem(source, destination, *args, **kwargs):
            if destination == "release" and kwargs.get("dst_dir_fd") is not None:
                staging = next(
                    path for path in self.layout.releases.iterdir()
                    if path.name.startswith(".staging-")
                )
                real_rename(staging, moved_staging)
                sentinel.write_text("keep", encoding="utf-8")
                raise OSError(errno.EXDEV, "forced cross-filesystem publication")
            return real_rename(source, destination, *args, **kwargs)

        with patch(
            "local_web_server.git_build.os.rename",
            side_effect=move_staging_then_force_cross_filesystem,
        ):
            with self.assertRaises(RuntimeLayoutError):
                Builder().build(
                    self.host, self.manifest(command), self.commit, self.layout, {}, self.log
                )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual([path.name for path in moved_staging.iterdir()], ["keep.txt"])
        self.assertFalse((self.layout.releases / self.commit).exists())
        self.assertEqual(list(self.layout.releases.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
