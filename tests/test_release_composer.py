import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_web_server.config import parse_manifest
from local_web_server.models import ReleaseEntry
from local_web_server.release_composer import ReleaseCompositionError, ReleaseComposer


class ReleaseComposerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "service-app"
        self.repository.mkdir()
        self.manifest = parse_manifest(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "service-app",
                    "title": "Service App",
                    "route": "/service-app",
                    "kind": "service",
                    "build": {
                        "commands": [["npm", "run", "build"]],
                        "output": "release",
                        "environment": [],
                        "release": [
                            {"source": "dist", "target": "public"},
                            {"source": "server/app.mjs", "target": "server/app.mjs"},
                            {"source": "data/model.csv", "target": "data/model.csv"},
                        ],
                    },
                    "healthPath": "/service-app/healthz",
                    "service": {
                        "module": "server/app.mjs",
                        "internalHealthPath": "/healthz",
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                    },
                }
            )
        )
        (self.repository / "dist/assets").mkdir(parents=True)
        (self.repository / "dist/index.html").write_text("<main>service</main>")
        (self.repository / "dist/assets/app.js").write_text("console.log('app')")
        (self.repository / "server").mkdir()
        (self.repository / "server/app.mjs").write_text("export const app = true;\n")
        (self.repository / "data").mkdir()
        (self.repository / "data/model.csv").write_text("season,value\n2026,1\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_assembles_exact_declared_tree_and_cleanly_replaces_stale_output(self):
        stale = self.repository / "release/stale.txt"
        stale.parent.mkdir()
        stale.write_text("remove me")

        output = ReleaseComposer().compose(self.repository, self.manifest.build)

        self.assertEqual(output, (self.repository / "release").resolve())
        self.assertEqual(
            sorted(
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file()
            ),
            ["data/model.csv", "public/assets/app.js", "public/index.html", "server/app.mjs"],
        )
        self.assertFalse(stale.exists())

    def test_missing_source_preserves_previous_release(self):
        previous = self.repository / "release/previous.txt"
        previous.parent.mkdir()
        previous.write_text("preserve")
        (self.repository / "data/model.csv").unlink()

        with self.assertRaisesRegex(ReleaseCompositionError, "release input is unavailable"):
            ReleaseComposer().compose(self.repository, self.manifest.build)

        self.assertEqual(previous.read_text(), "preserve")
        self.assertEqual(
            [path.name for path in self.repository.iterdir() if path.name.startswith(".release-")],
            [],
        )

    def test_repeated_composition_has_identical_paths_bytes_and_modes(self):
        composer = ReleaseComposer()
        first = composer.compose(self.repository, self.manifest.build)
        first_snapshot = self._snapshot(first)
        os.utime(self.repository / "dist/index.html", None)

        second = composer.compose(self.repository, self.manifest.build)

        self.assertEqual(self._snapshot(second), first_snapshot)

    def test_rejects_symlink_or_special_input_without_replacing_output(self):
        previous = self.repository / "release/previous.txt"
        previous.parent.mkdir()
        previous.write_text("preserve")
        private = self.repository / "private.txt"
        private.write_text("private")
        (self.repository / "data/model.csv").unlink()
        (self.repository / "data/model.csv").symlink_to(private)

        with self.assertRaisesRegex(ReleaseCompositionError, "release input is unavailable"):
            ReleaseComposer().compose(self.repository, self.manifest.build)

        self.assertEqual(previous.read_text(), "preserve")

    def test_rejects_release_input_beneath_symlinked_ancestor(self):
        previous = self.repository / "release/previous.txt"
        previous.parent.mkdir()
        previous.write_text("preserve")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private")
        (self.repository / "linked").symlink_to(outside, target_is_directory=True)
        build = replace(
            self.manifest.build,
            release_entries=(
                ReleaseEntry(Path("linked/private.txt"), Path("data/private.txt")),
            ),
        )

        with self.assertRaisesRegex(ReleaseCompositionError, "release input is unavailable"):
            ReleaseComposer().compose(self.repository, build)

        self.assertEqual(previous.read_text(), "preserve")
        self.assertNotIn("private", previous.read_text())

    def test_backup_cleanup_failure_keeps_activated_release_successful(self):
        previous = self.repository / "release/previous.txt"
        previous.parent.mkdir()
        previous.write_text("preserve")
        real_rmtree = shutil.rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if "-backup-" in Path(path).name:
                raise OSError("simulated cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        with patch(
            "local_web_server.release_composer.shutil.rmtree",
            side_effect=fail_backup_cleanup,
        ):
            output = ReleaseComposer().compose(self.repository, self.manifest.build)

        self.assertTrue((output / "public/index.html").is_file())
        self.assertFalse((output / "previous.txt").exists())
        backups = [
            path
            for path in self.repository.iterdir()
            if path.name.startswith(".release-backup-")
        ]
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "previous.txt").read_text(), "preserve")

    @staticmethod
    def _snapshot(root: Path):
        return tuple(
            (
                path.relative_to(root).as_posix(),
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )


if __name__ == "__main__":
    unittest.main()
