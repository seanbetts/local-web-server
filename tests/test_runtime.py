import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.runtime import (
    AppLock,
    DeploymentBusy,
    RuntimeLayout,
    atomic_symlink,
    prune_releases,
    read_release_commit,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.layout = RuntimeLayout(self.root, "plotter")

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_pointer_switch_and_pruning(self):
        old = self.layout.releases / ("a" * 40)
        new = self.layout.releases / ("b" * 40)
        stale = self.layout.releases / ("c" * 40)
        for path in (old, new, stale):
            path.mkdir(parents=True)

        atomic_symlink(self.layout.current, old)
        atomic_symlink(self.layout.previous, old)
        atomic_symlink(self.layout.current, new)
        prune_releases(self.layout)

        self.assertEqual(self.layout.current.resolve(), new)
        self.assertEqual(self.layout.previous.resolve(), old)
        self.assertFalse(stale.exists())

    def test_pruning_retains_protected_release_and_removes_ordinary_stale_release(self):
        current = self.layout.releases / ("a" * 40)
        protected = self.layout.releases / ("b" * 40)
        stale = self.layout.releases / ("c" * 40)
        for path in (current, protected, stale):
            path.mkdir(parents=True)
        atomic_symlink(self.layout.current, current)

        prune_releases(
            self.layout,
            protected_commits=(protected.name,),
        )

        self.assertTrue(current.is_dir())
        self.assertTrue(protected.is_dir())
        self.assertFalse(stale.exists())

    def test_pruning_rejects_invalid_protected_commit_without_deleting_releases(self):
        stale = self.layout.releases / ("c" * 40)
        stale.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "full Git object ID|protected"):
            prune_releases(
                self.layout,
                protected_commits=("not-a-full-commit",),
            )

        self.assertTrue(stale.is_dir())

    def test_pruning_rejects_missing_protected_release_without_deleting_stale(self):
        missing = "a" * 40
        stale = self.layout.releases / ("c" * 40)
        stale.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "protected|missing"):
            prune_releases(
                self.layout,
                protected_commits=(missing,),
            )

        self.assertTrue(stale.is_dir())

    def test_pruning_rejects_symlinked_protected_release_without_touching_targets(self):
        protected = self.layout.releases / ("a" * 40)
        stale = self.layout.releases / ("c" * 40)
        stale.mkdir(parents=True)
        external = self.root / "external-protected"
        external.mkdir()
        sentinel = external / "sentinel.bin"
        sentinel.write_bytes(b"external protected release")
        protected.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "release|real|protected"):
            prune_releases(
                self.layout,
                protected_commits=(protected.name,),
            )

        self.assertTrue(protected.is_symlink())
        self.assertTrue(stale.is_dir())
        self.assertEqual(sentinel.read_bytes(), b"external protected release")

    def test_read_release_commit_returns_only_a_valid_direct_release_target(self):
        release = self.layout.releases / ("a" * 64)
        release.mkdir(parents=True)
        atomic_symlink(self.layout.current, release)

        self.assertEqual(read_release_commit(self.layout.current), "a" * 64)

    def test_atomic_symlink_rejects_target_outside_its_releases_directory(self):
        self.layout.releases.mkdir(parents=True)
        outside = self.root / ("a" * 40)
        outside.mkdir()

        with self.assertRaisesRegex(ValueError, "releases"):
            atomic_symlink(self.layout.current, outside)

        self.assertFalse(self.layout.current.exists())

    def test_pruning_refuses_an_external_pointer_without_deleting_releases(self):
        stale = self.layout.releases / ("c" * 40)
        stale.mkdir(parents=True)
        external = self.root / "external"
        external.mkdir()
        self.layout.current.symlink_to(external)

        with self.assertRaisesRegex(ValueError, "releases"):
            prune_releases(self.layout)

        self.assertTrue(stale.exists())

    def test_pruning_rejects_a_symlinked_releases_directory_without_touching_external_releases(self):
        external_releases = self.root / "external-releases"
        external_commit = external_releases / ("c" * 40)
        external_commit.mkdir(parents=True)
        self.layout.app_root.mkdir(parents=True)
        self.layout.releases.symlink_to(external_releases, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "layout"):
            prune_releases(self.layout)

        self.assertTrue(external_commit.exists())

    def test_pruning_rejects_a_symlinked_app_root_without_touching_external_releases(self):
        external_app_root = self.root / "external-app"
        external_commit = external_app_root / "releases" / ("c" * 40)
        external_commit.mkdir(parents=True)
        self.layout.app_root.parent.mkdir(parents=True)
        self.layout.app_root.symlink_to(external_app_root, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "layout"):
            prune_releases(self.layout)

        self.assertTrue(external_commit.exists())

    def test_atomic_symlink_installs_a_canonical_target_when_intermediate_symlink_changes(self):
        commit = "a" * 40
        release = self.layout.releases / commit
        release.mkdir(parents=True)
        intermediate = self.root / "intermediate"
        intermediate.symlink_to(self.layout.releases, target_is_directory=True)

        atomic_symlink(self.layout.current, intermediate / commit)

        replacement = self.root / "replacement"
        (replacement / commit).mkdir(parents=True)
        intermediate.unlink()
        intermediate.symlink_to(replacement, target_is_directory=True)

        self.assertEqual(self.layout.current.resolve(), release)

    def test_pruning_refuses_a_regular_pointer_file_without_deleting_releases(self):
        stale = self.layout.releases / ("c" * 40)
        stale.mkdir(parents=True)
        self.layout.current.parent.mkdir(parents=True, exist_ok=True)
        self.layout.current.write_text("not a pointer", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "pointer"):
            prune_releases(self.layout)

        self.assertTrue(stale.exists())

    def test_second_process_reports_busy_while_app_lock_is_held(self):
        with AppLock(self.layout):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "from local_web_server.runtime import AppLock, DeploymentBusy, RuntimeLayout; "
                    "layout = RuntimeLayout(Path(__import__('sys').argv[1]), 'plotter'); "
                    "\ntry:\n"
                    "    with AppLock(layout): pass\n"
                    "except DeploymentBusy:\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(1)",
                    str(self.root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_release_commit_rejects_invalid_app_root_instead_of_reporting_absent(self):
        external = self.root / "external-app"
        (external / "releases").mkdir(parents=True)
        self.layout.app_root.parent.mkdir(parents=True)
        self.layout.app_root.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "runtime|layout|symlink"):
            read_release_commit(self.layout.current)

        self.assertEqual(list((external / "releases").iterdir()), [])

    def test_app_lock_rejects_symlinked_app_root_without_creating_external_lock(self):
        external = self.root / "external-app"
        external.mkdir()
        self.layout.app_root.parent.mkdir(parents=True)
        self.layout.app_root.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "runtime|layout|symlink"):
            with AppLock(self.layout):
                pass

        self.assertEqual(list(external.iterdir()), [])

    def test_app_lock_does_not_follow_a_symlinked_lock_file(self):
        self.layout.app_root.mkdir(parents=True)
        external = self.root / "external-lock"
        external.write_bytes(b"sentinel")
        self.layout.lock.symlink_to(external)

        with self.assertRaisesRegex(ValueError, "lock|symlink|runtime"):
            with AppLock(self.layout):
                pass

        self.assertEqual(external.read_bytes(), b"sentinel")

    def test_runtime_root_symlink_loop_is_a_domain_error(self):
        first = self.root / "loop-a"
        second = self.root / "loop-b"
        first.symlink_to(second, target_is_directory=True)
        second.symlink_to(first, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "runtime|layout|symlink"):
            layout = RuntimeLayout(first, "plotter")
            read_release_commit(layout.current)

    def test_pointer_symlink_loop_is_a_domain_error(self):
        self.layout.releases.mkdir(parents=True)
        self.layout.current.symlink_to("current")

        with self.assertRaisesRegex(ValueError, "pointer|symlink|runtime"):
            read_release_commit(self.layout.current)

    def test_pointer_removal_stays_on_pinned_app_root_during_symlink_swap(self):
        from local_web_server.runtime import remove_release_pointer

        commit = "a" * 40
        release = self.layout.releases / commit
        release.mkdir(parents=True)
        atomic_symlink(self.layout.previous, release)

        external_app = self.root / "external-app"
        external_release = external_app / "releases" / commit
        external_release.mkdir(parents=True)
        (external_app / "previous").symlink_to(f"releases/{commit}")
        moved_app = self.root / "moved-managed-app"

        import local_web_server.runtime as runtime_module

        real_pointer_commit = runtime_module._pointer_commit

        def swap_after_read(access, name):
            selected = real_pointer_commit(access, name)
            self.layout.app_root.rename(moved_app)
            self.layout.app_root.symlink_to(external_app, target_is_directory=True)
            return selected

        with patch(
            "local_web_server.runtime._pointer_commit", side_effect=swap_after_read
        ):
            remove_release_pointer(self.layout.previous, expected_commit=commit)

        self.assertFalse((moved_app / "previous").exists())
        self.assertTrue((external_app / "previous").is_symlink())
        self.assertTrue(external_release.is_dir())

    def test_protected_pruning_stays_on_pinned_releases_during_app_root_swap(self):
        current_commit = "a" * 40
        protected_commit = "b" * 40
        stale_commit = "c" * 40
        for commit in (current_commit, protected_commit, stale_commit):
            (self.layout.releases / commit).mkdir(parents=True)
        atomic_symlink(self.layout.current, self.layout.releases / current_commit)

        external_app = self.root / "external-app"
        external_releases = external_app / "releases"
        for commit in (current_commit, protected_commit, stale_commit):
            release = external_releases / commit
            release.mkdir(parents=True)
            (release / "sentinel.bin").write_bytes(f"external {commit}".encode())
        (external_app / "current").symlink_to(f"releases/{current_commit}")
        moved_app = self.root / "moved-managed-app"

        import local_web_server.runtime as runtime_module

        real_pointer_commit = runtime_module._pointer_commit
        swapped = False

        def swap_after_first_pointer_read(access, name):
            nonlocal swapped
            selected = real_pointer_commit(access, name)
            if not swapped:
                self.layout.app_root.rename(moved_app)
                self.layout.app_root.symlink_to(external_app, target_is_directory=True)
                swapped = True
            return selected

        with patch(
            "local_web_server.runtime._pointer_commit",
            side_effect=swap_after_first_pointer_read,
        ):
            prune_releases(
                self.layout,
                protected_commits=(protected_commit,),
            )

        self.assertTrue((moved_app / "releases" / current_commit).is_dir())
        self.assertTrue((moved_app / "releases" / protected_commit).is_dir())
        self.assertFalse((moved_app / "releases" / stale_commit).exists())
        for commit in (current_commit, protected_commit, stale_commit):
            sentinel = external_releases / commit / "sentinel.bin"
            self.assertEqual(sentinel.read_bytes(), f"external {commit}".encode())


if __name__ == "__main__":
    unittest.main()
