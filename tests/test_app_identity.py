import json
import tempfile
import unittest
from pathlib import Path

from local_web_server.app_identity import (
    AppIdentityCatalogue,
    IdentityAssignment,
    IdentityCatalogueError,
)
from local_web_server.colour import contrast_ratio


ROOT = Path(__file__).resolve().parents[1]


class AppIdentityCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = AppIdentityCatalogue.load(
            ROOT / "platform_assets" / "app-identities.json"
        )

    def test_search_returns_semantic_manifest_icons_without_reserved_fallback(self):
        report = self.catalogue.discover("meal food planning", ())

        names = [icon.name for icon in report.icons]
        self.assertIn("tools-kitchen-2", names)
        self.assertNotIn("apps", names)
        kitchen = next(icon for icon in report.icons if icon.name == "tools-kitchen-2")
        self.assertEqual(kitchen.source, "tools-kitchen-2")
        self.assertIn("meal", kitchen.keywords)

    def test_unfiltered_report_keeps_reserved_icons_visibly_separate(self):
        report = self.catalogue.discover(None, ())

        self.assertIn("apps", [icon.name for icon in report.icons])
        self.assertEqual(report.reserved_icons, ("apps",))

    def test_report_derives_accessible_colours_for_both_platform_modes(self):
        report = self.catalogue.discover(None, ())
        claret = next(accent for accent in report.accents if accent.name == "claret")

        self.assertEqual(claret.seed, "#7A1735")
        self.assertGreaterEqual(contrast_ratio(claret.light, "#FFFFFF"), 3.0)
        self.assertGreaterEqual(contrast_ratio(claret.dark, "#19202B"), 3.0)

    def test_assignments_expose_identity_without_repository_or_runtime_paths(self):
        assignment = IdentityAssignment(
            app_id="meals",
            title="Weekly Meals",
            icon="tools-kitchen-2",
            accent="#D97745",
        )

        report = self.catalogue.discover("meal", (assignment,))
        payload = report.as_dict()

        self.assertEqual(
            payload["assignments"],
            [
                {
                    "appId": "meals",
                    "title": "Weekly Meals",
                    "icon": "tools-kitchen-2",
                    "accent": "#D97745",
                }
            ],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("repository", serialized.lower())
        self.assertNotIn("runtime", serialized.lower())

    def test_loader_rejects_duplicate_names_and_invalid_accent_seeds(self):
        valid = json.loads(
            (ROOT / "platform_assets" / "app-identities.json").read_text(
                encoding="utf-8"
            )
        )
        cases = []
        duplicate = json.loads(json.dumps(valid))
        duplicate["icons"].append(dict(duplicate["icons"][0]))
        cases.append(duplicate)
        invalid_accent = json.loads(json.dumps(valid))
        invalid_accent["accents"][0]["seed"] = "#7a1735"
        cases.append(invalid_accent)

        for index, payload in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "identities.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(IdentityCatalogueError):
                    AppIdentityCatalogue.load(path)


if __name__ == "__main__":
    unittest.main()
