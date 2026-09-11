import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch


class SystemIndexBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.dist = Path(self.temporary.name) / "dist"
        assets = self.dist / "assets"
        assets.mkdir(parents=True)
        self.home = b"<!doctype html><script src='/_local-web/platform/index/assets/index-b.js'></script>"
        (self.dist / "index.html").write_bytes(self.home)
        (assets / "index-b.js").write_bytes(b"console.log('index')")
        (assets / "index-a.css").write_bytes(b"body { color: black; }")

    def tearDown(self):
        self.temporary.cleanup()

    def load(self):
        from local_web_server.system_index_bundle import load_system_index_bundle

        return load_system_index_bundle(self.dist)

    def assert_invalid(self):
        with self.assertRaisesRegex(ValueError, "^System Index bundle is invalid$"):
            self.load()

    def test_loads_exact_home_and_sorted_immediate_assets(self):
        bundle = self.load()

        self.assertEqual(bundle.home, self.home)
        self.assertEqual(
            bundle.assets,
            (
                (PurePosixPath("assets/index-a.css"), b"body { color: black; }"),
                (PurePosixPath("assets/index-b.js"), b"console.log('index')"),
            ),
        )

    def test_rejects_missing_index(self):
        (self.dist / "index.html").unlink()

        self.assert_invalid()

    def test_rejects_symlinked_index(self):
        (self.dist / "index.html").unlink()
        (self.dist / "index.html").symlink_to(self.dist / "assets" / "index-b.js")

        self.assert_invalid()

    def test_rejects_directory_where_asset_file_is_expected(self):
        (self.dist / "assets" / "nested.js").mkdir()

        self.assert_invalid()

    def test_rejects_asset_outside_assets_directory(self):
        (self.dist / "outside.js").write_bytes(b"console.log('outside')")

        self.assert_invalid()

    def test_rejects_symlinked_asset(self):
        (self.dist / "assets" / "linked.js").symlink_to(
            self.dist / "assets" / "index-b.js"
        )

        self.assert_invalid()

    def test_rejects_symlinked_assets_directory(self):
        real_assets = self.dist / "real-assets"
        (self.dist / "assets").rename(real_assets)
        (self.dist / "assets").symlink_to(real_assets)

        self.assert_invalid()

    def test_rejects_extra_asset_extension(self):
        (self.dist / "assets" / "source.map").write_bytes(b"{}")

        self.assert_invalid()

    def test_requires_a_javascript_asset(self):
        (self.dist / "assets" / "index-b.js").unlink()

        self.assert_invalid()

    def test_rejects_bundle_larger_than_eight_mebibytes(self):
        (self.dist / "assets" / "index-b.js").write_bytes(b"x" * (8 * 1024 * 1024))

        self.assert_invalid()

    def test_rejects_over_limit_metadata_before_reading_content(self):
        with (self.dist / "assets" / "index-b.js").open("wb") as output:
            output.truncate(8 * 1024 * 1024)

        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("oversized content was read"),
        ) as read_bytes:
            self.assert_invalid()

        read_bytes.assert_not_called()


class UiGalleryBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.dist = Path(self.temporary.name) / "dist"
        assets = self.dist / "assets"
        assets.mkdir(parents=True)
        (self.dist / "index.html").write_bytes(b"<script src='/assets/main.js'></script>")
        (self.dist / "index-frame.html").write_bytes(b"<script src='/assets/main.js'></script>")
        (assets / "main.js").write_bytes(b"console.log('gallery')")
        (assets / "main.css").write_bytes(b"body {}")

    def tearDown(self):
        self.temporary.cleanup()

    def load(self):
        from local_web_server.system_index_bundle import load_ui_gallery_bundle

        return load_ui_gallery_bundle(self.dist)

    def test_loads_all_gallery_files_in_sorted_order(self):
        self.assertEqual(
            self.load(),
            (
                (PurePosixPath("assets/main.css"), b"body {}"),
                (PurePosixPath("assets/main.js"), b"console.log('gallery')"),
                (PurePosixPath("index-frame.html"), b"<script src='/assets/main.js'></script>"),
                (PurePosixPath("index.html"), b"<script src='/assets/main.js'></script>"),
            ),
        )

    def test_rejects_gallery_symlinks(self):
        (self.dist / "index-frame.html").unlink()
        (self.dist / "index-frame.html").symlink_to(self.dist / "index.html")

        with self.assertRaisesRegex(ValueError, "^UI Gallery bundle is invalid$"):
            self.load()


if __name__ == "__main__":
    unittest.main()
