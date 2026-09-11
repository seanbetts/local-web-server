import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_update_models import FileChange
from local_web_server.app_update_transaction import (
    AppUpdateTransactionError,
    UpdatePublisher,
)


ROOT = Path(__file__).parents[1]


def _git_path(repository: Path, relative: str) -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--git-path", relative),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repository / path


class _FailOnSecondReplace:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, source: Path, destination: Path) -> None:
        self.calls += 1
        if self.calls == 2:
            raise OSError("private publication detail")
        os.replace(source, destination)


class _InterruptAfterFirstReplace:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, source: Path, destination: Path) -> None:
        self.calls += 1
        if self.calls == 2:
            raise KeyboardInterrupt
        os.replace(source, destination)


class UpdatePublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.repository = Path(self.temporary.name) / "application"
        self.repository.mkdir()
        (self.repository / "index.html").write_bytes(b"old index\n")
        (self.repository / "package.json").write_bytes(b'{"old":true}\n')
        (self.repository / "unrelated.txt").write_bytes(b"preserve me\n")
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
                "baseline",
            ),
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        self.changes = (
            FileChange(Path("index.html"), b"old index\n", b"new index\n"),
            FileChange(
                Path("package.json"),
                b'{"old":true}\n',
                b'{"new":true}\n',
            ),
            FileChange(Path("vendor/local-web-ui.tgz"), None, b"reviewed artifact"),
        )
        self.recovery_path = _git_path(
            self.repository, "local-web/app-update-recovery.json"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot_targets(self) -> dict[Path, bytes | None]:
        snapshot: dict[Path, bytes | None] = {}
        for change in self.changes:
            destination = self.repository / change.path
            snapshot[change.path] = (
                destination.read_bytes() if destination.is_file() else None
            )
        return snapshot

    def test_success_publishes_exact_planned_files_and_clears_recovery(self):
        validated = []
        publisher = UpdatePublisher()

        publisher.publish(
            self.repository,
            self.changes,
            lambda: validated.append(self.snapshot_targets()),
        )

        self.assertEqual(
            self.snapshot_targets(),
            {change.path: change.after for change in self.changes},
        )
        self.assertEqual(
            validated,
            [{change.path: change.after for change in self.changes}],
        )
        self.assertEqual(
            (self.repository / "unrelated.txt").read_bytes(), b"preserve me\n"
        )
        self.assertFalse(publisher.recovery_pending(self.repository))

    def test_mid_publication_failure_restores_exact_original_tree(self):
        before = self.snapshot_targets()
        publisher = UpdatePublisher(replace=_FailOnSecondReplace())

        with self.assertRaisesRegex(AppUpdateTransactionError, "publication failed"):
            publisher.publish(self.repository, self.changes, lambda: None)

        self.assertEqual(self.snapshot_targets(), before)
        self.assertFalse((self.repository / "vendor").exists())
        self.assertFalse(publisher.recovery_pending(self.repository))

    def test_nested_foundation_failure_removes_only_new_empty_parents(self):
        changes = (
            FileChange(Path("src/App.tsx"), None, b"app\n"),
            FileChange(Path("src/test/setup.ts"), None, b"setup\n"),
        )
        source = self.repository / "src"
        source.mkdir()
        (source / "domain.py").write_bytes(b"preserve\n")
        publisher = UpdatePublisher(replace=_FailOnSecondReplace())

        with self.assertRaisesRegex(AppUpdateTransactionError, "publication failed"):
            publisher.publish(self.repository, changes, lambda: None)

        self.assertEqual((source / "domain.py").read_bytes(), b"preserve\n")
        self.assertFalse((source / "App.tsx").exists())
        self.assertFalse((source / "test").exists())
        self.assertFalse(publisher.recovery_pending(self.repository))

    def test_validation_failure_restores_exact_original_tree(self):
        before = self.snapshot_targets()
        publisher = UpdatePublisher()

        def reject() -> None:
            raise ValueError(f"private validation detail {self.repository}")

        with self.assertRaisesRegex(
            AppUpdateTransactionError, "publication failed"
        ) as raised:
            publisher.publish(self.repository, self.changes, reject)

        self.assertEqual(self.snapshot_targets(), before)
        self.assertNotIn("private validation detail", str(raised.exception))
        self.assertNotIn(str(self.repository), str(raised.exception))
        self.assertFalse(publisher.recovery_pending(self.repository))

    def test_publish_can_restore_a_created_target_to_absence(self):
        path = Path("vendor/local-web-ui.tgz")
        target = self.repository / path
        publisher = UpdatePublisher()

        publisher.publish(
            self.repository,
            (FileChange(path, None, b"generated\n"),),
            lambda: self.assertEqual(target.read_bytes(), b"generated\n"),
        )
        publisher.publish(
            self.repository,
            (FileChange(path, b"generated\n", None),),
            lambda: self.assertFalse(target.exists()),
        )

        self.assertFalse(target.exists())
        self.assertFalse(self.recovery_path.exists())
        self.assertEqual(
            (self.repository / "unrelated.txt").read_bytes(), b"preserve me\n"
        )

    def test_inverse_publish_failures_restore_the_forward_target_exactly(self):
        for failure in ("unlink", "validation", "base-exception"):
            with self.subTest(failure=failure):
                path = Path(f"vendor/{failure}.tgz")
                target = self.repository / path
                publisher = UpdatePublisher()
                publisher.publish(
                    self.repository,
                    (FileChange(path, None, b"generated\n"),),
                    lambda: None,
                )
                target.chmod(0o640)
                original_mode = stat.S_IMODE(target.stat().st_mode)
                inverse = (FileChange(path, b"generated\n", None),)
                attempted: list[str] = []

                if failure == "unlink":
                    original_unlink = Path.unlink

                    def fail_target_unlink(candidate: Path, *args, **kwargs) -> None:
                        if candidate == target:
                            attempted.append("unlink")
                            raise OSError("private unlink detail")
                        original_unlink(candidate, *args, **kwargs)

                    with patch.object(Path, "unlink", fail_target_unlink):
                        with self.assertRaisesRegex(
                            AppUpdateTransactionError, "publication failed"
                        ):
                            publisher.publish(self.repository, inverse, lambda: None)
                else:
                    error = (
                        KeyboardInterrupt()
                        if failure == "base-exception"
                        else ValueError("private validation detail")
                    )

                    def reject() -> None:
                        attempted.append("validate")
                        raise error

                    expected = (
                        KeyboardInterrupt
                        if failure == "base-exception"
                        else AppUpdateTransactionError
                    )
                    with self.assertRaises(expected):
                        publisher.publish(self.repository, inverse, reject)

                self.assertEqual(
                    attempted, ["unlink" if failure == "unlink" else "validate"]
                )
                if failure == "base-exception":
                    self.assertTrue(self.recovery_path.is_file())
                    self.assertTrue(UpdatePublisher().recover(self.repository))
                else:
                    self.assertFalse(self.recovery_path.exists())
                self.assertEqual(target.read_bytes(), b"generated\n")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), original_mode)
                self.assertEqual(
                    list(target.parent.glob(f".{target.name}.local-web-update-*")), []
                )
                self.assertEqual(
                    (self.repository / "unrelated.txt").read_bytes(), b"preserve me\n"
                )
                self.assertFalse(self.recovery_path.exists())

    def test_recover_restores_an_interrupted_operation_before_next_update(self):
        before = self.snapshot_targets()
        publisher = UpdatePublisher(replace=_InterruptAfterFirstReplace())

        with self.assertRaises(KeyboardInterrupt):
            publisher.publish(self.repository, self.changes, lambda: None)

        marker = _git_path(self.repository, "local-web/app-update-recovery.json")
        self.assertTrue(marker.is_file())
        self.assertTrue(publisher.recovery_pending(self.repository))
        self.assertTrue(UpdatePublisher().recover(self.repository))
        self.assertEqual(self.snapshot_targets(), before)
        self.assertFalse((self.repository / "vendor").exists())
        self.assertFalse(UpdatePublisher().recovery_pending(self.repository))

    def test_publish_rejects_noncanonical_relative_paths_before_writing(self):
        invalid_changes = (
            FileChange(Path("../outside"), None, b"private"),
        )
        publisher = UpdatePublisher()

        with self.assertRaisesRegex(AppUpdateTransactionError, "publication failed"):
            publisher.publish(self.repository, invalid_changes, lambda: None)

        self.assertFalse((self.repository.parent / "outside").exists())
        self.assertFalse(publisher.recovery_pending(self.repository))


if __name__ == "__main__":
    unittest.main()
