import subprocess
import unittest
from pathlib import Path

from local_web_server.icons import (
    ICON_PATHS,
    canonical_manifest_icon,
)


ROOT = Path(__file__).resolve().parents[1]


class IconCatalogueTests(unittest.TestCase):
    def test_catalogue_geometry_is_safe_and_generated_file_is_current(self):
        for name, fragments in ICON_PATHS.items():
            with self.subTest(name=name):
                self.assertTrue(fragments.startswith("<path "))
                self.assertNotIn("<svg", fragments)
                self.assertNotIn("script", fragments.lower())

        result = subprocess.run(
            ["node", "scripts/build_icon_catalogue.mjs", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_legacy_manifest_aliases_resolve_to_allowlisted_icons(self):
        self.assertEqual(canonical_manifest_icon("app"), "apps")
        self.assertEqual(canonical_manifest_icon("shirt"), "shirt-sport")
        self.assertEqual(canonical_manifest_icon("chart"), "chart-line")
        self.assertEqual(canonical_manifest_icon("book"), "book")
        with self.assertRaisesRegex(ValueError, "unknown icon"):
            canonical_manifest_icon("<svg/>")


if __name__ == "__main__":
    unittest.main()
