import json
import subprocess
import unittest
from pathlib import Path

from local_web_server.icons import (
    ACTION_ICON_NAMES,
    ICON_PATHS,
    MANIFEST_ICON_NAMES,
    canonical_manifest_icon,
)


ROOT = Path(__file__).resolve().parents[1]


class IconCatalogueTests(unittest.TestCase):
    def test_generated_catalogue_has_the_complete_explicit_allowlists(self):
        payload = json.loads(
            (ROOT / "platform_assets" / "icons.json").read_text(encoding="utf-8")
        )

        self.assertEqual(payload["package"], "@tabler/icons-react")
        self.assertEqual(payload["version"], "3.46.0")
        self.assertEqual(
            MANIFEST_ICON_NAMES,
            (
                "apps",
                "book",
                "briefcase",
                "calendar",
                "chart-line",
                "checklist",
                "database",
                "home",
                "map",
                "music",
                "photo",
                "plane",
                "route",
                "shirt-sport",
                "shopping-cart",
                "stretching",
                "target",
                "ticket",
                "tools-kitchen-2",
                "wallet",
                "weather-sun",
            ),
        )
        self.assertEqual(
            ACTION_ICON_NAMES,
            (
                "arrow-down",
                "arrow-up",
                "camera",
                "car",
                "caravan",
                "check",
                "chevron-down",
                "chevron-left",
                "chevron-right",
                "chevron-up",
                "circle-alert",
                "copy",
                "download",
                "grip-vertical",
                "image",
                "loader",
                "map-pin",
                "minus",
                "paperclip",
                "pencil",
                "plus",
                "refresh",
                "search",
                "ship",
                "signpost",
                "trash",
                "truck",
                "x",
            ),
        )
        self.assertEqual(tuple(payload["manifest"]), MANIFEST_ICON_NAMES)
        self.assertEqual(tuple(payload["actions"]), ACTION_ICON_NAMES)
        self.assertEqual(set(payload["icons"]), set(ICON_PATHS))
        self.assertIn("external-link", payload["internal"])

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
