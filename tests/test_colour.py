import json
import unittest
from pathlib import Path

from local_web_server.colour import contrast_ratio, derive_accessible_accent


class ColourTests(unittest.TestCase):
    def test_shared_accent_fixtures_are_deterministic(self):
        fixture_path = Path(__file__).parents[1] / "platform_assets" / "accent-fixtures.json"
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))

        for fixture in fixtures:
            with self.subTest(seed=fixture["seed"], surface=fixture["surface"]):
                result = derive_accessible_accent(fixture["seed"], fixture["surface"])
                self.assertEqual(result, fixture["expected"])
                self.assertGreaterEqual(contrast_ratio(result, fixture["surface"]), 3.0)

    def test_seed_is_returned_when_it_already_has_required_contrast(self):
        self.assertEqual(derive_accessible_accent("#000000", "#FFFFFF"), "#000000")

    def test_rejects_non_canonical_colours_without_disclosing_them(self):
        for invalid in ("red", "#fff", "#abcdef", "#GG0000", " #000000"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "invalid colour"
            ) as caught:
                derive_accessible_accent(invalid, "#FFFFFF")
            self.assertNotIn(invalid, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
