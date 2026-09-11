import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_initialization import (
    AppInitializationError,
    ExistingEmptyDestination,
)


ROOT = Path(__file__).parents[1]


class ExistingEmptyDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.workspace = Path(self.temporary.name)
        self.publisher = ExistingEmptyDestination(token_factory=lambda: "abc123")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_platform_stage(stage: Path, *, app_id: str) -> None:
        manifest = {
            "schemaVersion": 1,
            "id": app_id,
            "title": "Recovery fixture",
            "route": f"/{app_id}",
            "kind": "static",
            "build": {
                "commands": [["npm", "ci"], ["npm", "run", "build"]],
                "output": "dist",
                "environment": ["VITE_PUBLIC_BASE_PATH"],
            },
            "healthPath": f"/{app_id}/",
            "platform": {
                "contractVersion": 1,
                "templateVersion": 1,
                "uiVersion": "0.4.0",
                "capabilities": [],
            },
        }
        provenance = {
            "schemaVersion": 1,
            "templateVersion": 1,
            "platformContractVersion": 1,
            "ui": {"version": "0.4.0", "sha256": "a" * 64},
            "capabilities": [],
            "domainPaletteTokens": [],
            "managedFiles": [],
        }
        (stage / "local-web.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (stage / ".local-web-platform.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )

    def test_resolves_an_existing_real_empty_directory(self):
        """Rejecting every existing path would make app init unusable."""
        destination = self.workspace / "recipes"
        destination.mkdir()

        self.assertEqual(self.publisher.resolve(destination), destination.resolve())

    def test_builds_paired_exact_sibling_names_from_the_operation_token(self):
        """Rejecting a safe opaque token would break deterministic publication paths."""
        destination = self.workspace / "recipes"
        paths = ExistingEmptyDestination(
            token_factory=lambda: "fixed-token"
        ).paths(destination)

        self.assertEqual(paths.destination, destination)
        self.assertEqual(
            paths.stage,
            self.workspace / ".recipes.local-web-init-fixed-token.stage",
        )
        self.assertEqual(
            paths.empty_backup,
            self.workspace / ".recipes.local-web-init-fixed-token.empty",
        )

    def test_rejects_missing_file_symlink_root_and_non_empty_destinations(self):
        """Accepting an unsafe destination could overwrite user-owned state."""
        missing = self.workspace / "missing"
        file_destination = self.workspace / "file"
        file_destination.write_text("preserve\n", encoding="utf-8")
        target = self.workspace / "target"
        target.mkdir()
        symlink = self.workspace / "symlink"
        symlink.symlink_to(target, target_is_directory=True)
        non_empty = self.workspace / "recipes"
        non_empty.mkdir()
        owner = non_empty / "owner.txt"
        owner.write_text("preserve\n", encoding="utf-8")

        for destination in (missing, file_destination, symlink, Path(destination_anchor())):
            with self.subTest(destination=destination), self.assertRaises(
                AppInitializationError
            ):
                self.publisher.resolve(destination)
        with self.assertRaisesRegex(
            AppInitializationError, "destination must be empty"
        ):
            self.publisher.resolve(non_empty)

        self.assertEqual(file_destination.read_text(encoding="utf-8"), "preserve\n")
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(owner.read_text(encoding="utf-8"), "preserve\n")

    def test_recovers_an_absent_destination_from_one_empty_backup(self):
        """A crash after the first publication rename must not strand the folder."""
        destination = self.workspace / "recipes"
        paths = self.publisher.paths(destination)
        paths.empty_backup.mkdir()

        self.publisher.recover(destination)

        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(paths.empty_backup.exists())

    def test_removes_only_the_empty_backup_after_completed_publication(self):
        """A crash after the second rename must retain the complete public app."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        completed = destination / "package.json"
        completed.write_text("{}\n", encoding="utf-8")
        paths = self.publisher.paths(destination)
        paths.empty_backup.mkdir()

        self.publisher.recover(destination)

        self.assertEqual(completed.read_text(encoding="utf-8"), "{}\n")
        self.assertFalse(paths.empty_backup.exists())

    def test_discards_a_paired_stage_and_restores_the_empty_destination(self):
        """A crash between publication renames must restore the original state."""
        destination = self.workspace / "recipes"
        paths = self.publisher.paths(destination)
        paths.stage.mkdir()
        self._write_platform_stage(paths.stage, app_id="recipes")
        paths.empty_backup.mkdir()

        self.publisher.recover(destination)

        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(paths.stage.exists())
        self.assertFalse(paths.empty_backup.exists())

    def test_refuses_unproven_partial_and_wrong_destination_stages_without_mutation(self):
        """A platform-looking sibling must prove exact provenance before deletion."""
        cases = ("arbitrary", "partial", "wrong-id")
        for label in cases:
            with self.subTest(label=label):
                destination = self.workspace / f"recipes-{label}"
                publisher = ExistingEmptyDestination(token_factory=lambda: label)
                paths = publisher.paths(destination)
                paths.stage.mkdir()
                paths.empty_backup.mkdir()
                if label == "arbitrary":
                    (paths.stage / "owner.txt").write_text(
                        "preserve\n", encoding="utf-8"
                    )
                elif label == "partial":
                    self._write_platform_stage(
                        paths.stage, app_id=destination.name
                    )
                    (paths.stage / ".local-web-platform.json").unlink()
                else:
                    self._write_platform_stage(paths.stage, app_id="other-app")

                before = {
                    path.relative_to(paths.stage): path.read_bytes()
                    for path in paths.stage.rglob("*")
                    if path.is_file()
                }
                with self.assertRaisesRegex(
                    AppInitializationError, "recovery is unsafe"
                ):
                    publisher.recover(destination)

                self.assertFalse(destination.exists())
                self.assertTrue(paths.stage.is_dir())
                self.assertTrue(paths.empty_backup.is_dir())
                self.assertEqual(
                    {
                        path.relative_to(paths.stage): path.read_bytes()
                        for path in paths.stage.rglob("*")
                        if path.is_file()
                    },
                    before,
                )

    def test_backup_removal_failure_rolls_publication_back_to_empty_destination(self):
        """A late backup cleanup failure must not leave the staged app public."""
        destination = self.workspace / "recipes"
        destination.mkdir()
        paths = self.publisher.paths(destination)
        paths.stage.mkdir()
        generated = paths.stage / "package.json"
        generated.write_text("{}\n", encoding="utf-8")
        real_rmdir = Path.rmdir

        def fail_backup_removal(path: Path):
            if Path(path) == paths.empty_backup:
                raise OSError("private backup removal failure")
            return real_rmdir(path)

        with patch.object(Path, "rmdir", fail_backup_removal), self.assertRaisesRegex(
            AppInitializationError, "app publication failed"
        ) as raised:
            self.publisher.publish(paths.stage, destination)

        self.assertNotIn("private backup removal failure", str(raised.exception))
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(generated.read_text(encoding="utf-8"), "{}\n")
        self.assertFalse(paths.empty_backup.exists())

    def test_refuses_ambiguous_operations_without_mutation(self):
        """Recovery must not guess between multiple interrupted operations."""
        destination = self.workspace / "recipes"
        first = ExistingEmptyDestination(token_factory=lambda: "first").paths(destination)
        second = ExistingEmptyDestination(token_factory=lambda: "second").paths(destination)
        first.empty_backup.mkdir()
        second.empty_backup.mkdir()

        with self.assertRaisesRegex(AppInitializationError, "recovery is ambiguous"):
            self.publisher.recover(destination)

        self.assertFalse(destination.exists())
        self.assertTrue(first.empty_backup.is_dir())
        self.assertTrue(second.empty_backup.is_dir())

    def test_refuses_a_non_empty_backup_without_mutation(self):
        """Recovery must never promote or delete a backup containing user data."""
        destination = self.workspace / "recipes"
        paths = self.publisher.paths(destination)
        paths.empty_backup.mkdir()
        owner = paths.empty_backup / "owner.txt"
        owner.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(AppInitializationError, "recovery is unsafe"):
            self.publisher.recover(destination)

        self.assertFalse(destination.exists())
        self.assertEqual(owner.read_text(encoding="utf-8"), "preserve\n")


def destination_anchor() -> str:
    return Path.cwd().anchor


if __name__ == "__main__":
    unittest.main()
