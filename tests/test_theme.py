import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import local_web_server.theme as theme_module
from local_web_server.theme import ThemeError, ThemeStore, validate_theme


_FALLBACK_LAYER_PREFIX = b"@layer lwp-fallback {\n"
_FALLBACK_LAYER_SUFFIX = b"}\n"


def _validate_fallback_theme(content: bytes) -> str:
    if (
        not isinstance(content, bytes)
        or not content.startswith(_FALLBACK_LAYER_PREFIX)
        or not content.endswith(_FALLBACK_LAYER_SUFFIX)
    ):
        raise ThemeError("theme validation failed")
    return validate_theme(
        content[len(_FALLBACK_LAYER_PREFIX) : -len(_FALLBACK_LAYER_SUFFIX)]
    )


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime_root = self.root / "runtime"
        self.valid = (Path(__file__).parents[1] / "platform_assets" / "theme.css").read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_validates_both_modes_tokens_and_registered_accents(self):
        digest = validate_theme(
            self.valid,
            ("#75A7FF", "#D98AA8", "#76D39B", "#8EA7C6"),
        )
        self.assertEqual(len(digest), 64)

    def test_requires_central_compact_typography_and_motion_tokens(self):
        candidate = self.valid

        self.assertEqual(len(validate_theme(candidate)), 64)

        for invalid in (
            candidate.replace(
                b"--lwp-text-size-compact: .75rem",
                b"--lwp-text-size-compact: 12pt",
                1,
            ),
            candidate.replace(
                b"--lwp-motion-duration-fast: 120ms",
                b"--lwp-motion-duration-fast: instant",
                1,
            ),
            candidate.replace(
                b"--lwp-motion-easing-standard: ease",
                b"--lwp-motion-easing-standard: spring",
                1,
            ),
        ):
            with self.subTest(invalid=invalid[:120]), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ):
                validate_theme(invalid)

    def test_requires_a_validated_foundation_border_width(self):
        declaration = b"  --lwp-border-width: 1px;\n"
        without_border = self.valid.replace(declaration, b"")

        def candidate(value):
            return without_border.replace(
                b"  --lwp-focus-width: 3px;\n",
                b"  --lwp-border-width: " + value + b";\n  --lwp-focus-width: 3px;\n",
            )

        for valid in (b"1px", b"4px", b".0625rem", b".25rem"):
            with self.subTest(valid=valid):
                self.assertEqual(len(validate_theme(candidate(valid))), 64)
        for invalid in (b"0", b"0px", b".5px", b"4.1px", b".05rem", b".3rem"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ):
                validate_theme(candidate(invalid))
        with self.assertRaisesRegex(ThemeError, "theme validation failed"):
            validate_theme(without_border)

    def test_cubic_bezier_easing_constrains_only_the_x_coordinates(self):
        candidate = self.valid

        for easing in (
            b"cubic-bezier(0, -2, 1, 3)",
            b"cubic-bezier(1, 3, 0, -2)",
        ):
            with self.subTest(easing=easing):
                valid = candidate.replace(b"ease", easing, 1)
                self.assertEqual(len(validate_theme(valid)), 64)

        for easing in (
            b"cubic-bezier(-.1, 0, 1, 1)",
            b"cubic-bezier(1.1, 0, 1, 1)",
            b"cubic-bezier(0, 0, -.1, 1)",
            b"cubic-bezier(0, 0, 1.1, 1)",
            b"cubic-bezier(2, 0, 2, 1)",
            b"cubic-bezier(2,0,2,1)",
        ):
            with self.subTest(easing=easing), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ):
                validate_theme(candidate.replace(b"ease", easing, 1))

    def test_live_and_fallback_foundation_tokens_stay_in_parity(self):
        fallback = (
            Path(__file__).parents[1] / "packages" / "ui" / "src" / "fallback-theme.css"
        ).read_text(encoding="utf-8")
        live = self.valid.decode("utf-8")

        self.assertEqual(len(_validate_fallback_theme(fallback.encode("utf-8"))), 64)

        def root_declarations(stylesheet):
            block = stylesheet.split(":root {", 1)[1].split("}", 1)[0]
            return {
                name.strip(): value.strip()
                for declaration in block.split(";")
                if declaration.strip()
                for name, value in (declaration.split(":", 1),)
            }

        self.assertEqual(root_declarations(live), root_declarations(fallback))

        for declaration in (
            "--lwp-text-size-compact: .75rem;",
            "--lwp-motion-duration-fast: 120ms;",
            "--lwp-motion-easing-standard: ease;",
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(live.count(declaration), 1)
                self.assertEqual(fallback.count(declaration), 1)

    def test_fallback_theme_rejects_same_line_structural_selector(self):
        fallback = (
            Path(__file__).parents[1] / "packages" / "ui" / "src" / "fallback-theme.css"
        ).read_bytes()
        invalid = fallback.removesuffix(b"}\n") + b"  body { color: red; }\n}\n"

        with self.assertRaisesRegex(ThemeError, "theme validation failed"):
            _validate_fallback_theme(invalid)

    def test_fallback_theme_rejects_unexpected_at_rule(self):
        fallback = (
            Path(__file__).parents[1] / "packages" / "ui" / "src" / "fallback-theme.css"
        ).read_bytes()
        invalid = fallback.removesuffix(b"}\n") + b"  @media print {}\n}\n"

        with self.assertRaisesRegex(ThemeError, "theme validation failed"):
            _validate_fallback_theme(invalid)

    def test_rejects_missing_selector_or_token(self):
        for invalid in (
            self.valid.replace(b':root[data-lwp-colour-mode="dark"]', b':root[data-mode="dark"]'),
            self.valid.replace(b"--lwp-colour-text:", b"--lwp-colour-copy:"),
        ):
            with self.subTest(invalid=invalid[:30]), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ):
                validate_theme(invalid)

    def test_rejects_urls_imports_app_selectors_and_unknown_declarations(self):
        invalid_values = (
            self.valid + b'\n:root { --lwp-shadow: url("https://private.example"); }\n',
            b'@import "private.css";\n' + self.valid,
            self.valid + b"\n.plotter { --lwp-colour-canvas: #FFFFFF; }\n",
            self.valid.replace(b"color-scheme:", b"background:"),
        )
        for invalid in invalid_values:
            with self.subTest(kind=invalid[-50:]), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ) as caught:
                validate_theme(invalid)
            self.assertNotIn("private.example", str(caught.exception))

    def test_rejects_comment_split_and_escaped_url_tokens(self):
        for spelling in (
            b'u/**/rl("https://private.example/pixel")',
            b'u\\72l("https://private.example/pixel")',
        ):
            invalid = self.valid.replace(
                b"0 12px 32px rgba(23, 32, 51, .14)", spelling, 1
            )
            with self.subTest(spelling=spelling), self.assertRaisesRegex(
                ThemeError, "theme validation failed"
            ):
                validate_theme(invalid)

    def test_root_system_values_must_match_the_validated_explicit_modes(self):
        invalid = self.valid.replace(
            b"light-dark(#172033, #F5F7FA)",
            b"light-dark(#F6F7F9, #11151C)",
            1,
        )

        with self.assertRaisesRegex(ThemeError, "theme validation failed"):
            validate_theme(invalid)

    def test_activate_retains_previous_theme_and_uses_private_regular_files(self):
        store = ThemeStore(self.runtime_root)
        updated = self.valid.replace(b"#F6F7F9", b"#F5F6F8")

        first = store.activate(self.valid)
        second = store.activate(updated)

        self.assertNotEqual(first, second)
        self.assertEqual(store.current.read_bytes(), updated)
        self.assertEqual(store.previous.read_bytes(), self.valid)
        self.assertEqual(store.current.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.previous.stat().st_mode & 0o777, 0o600)

    def test_failed_validation_does_not_touch_runtime_state(self):
        store = ThemeStore(self.runtime_root)
        store.activate(self.valid)
        before = store.current.read_bytes()

        with self.assertRaises(ThemeError):
            store.activate(b"private invalid theme bytes")

        self.assertEqual(store.current.read_bytes(), before)
        self.assertFalse(store.previous.exists())

    def test_rollback_atomically_swaps_current_and_previous(self):
        store = ThemeStore(self.runtime_root)
        updated = self.valid.replace(b"#F6F7F9", b"#F5F6F8")
        store.activate(self.valid)
        store.activate(updated)

        digest = store.rollback()

        self.assertEqual(store.current.read_bytes(), self.valid)
        self.assertEqual(store.previous.read_bytes(), updated)
        self.assertEqual(digest, validate_theme(self.valid))

    def test_activate_restores_exact_pair_at_every_write_failure_boundary(self):
        store = ThemeStore(self.runtime_root)
        updated = self.valid.replace(b"#F6F7F9", b"#F5F6F8")
        store.activate(self.valid)
        store.activate(updated)
        before = (store.current.read_bytes(), store.previous.read_bytes())
        candidate = self.valid.replace(b"#F6F7F9", b"#F4F5F7")

        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                original = theme_module._atomic_write_at
                calls = 0

                def fail_once(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("private injected diagnostic")
                    return original(*args, **kwargs)

                with patch("local_web_server.theme._atomic_write_at", side_effect=fail_once):
                    with self.assertRaisesRegex(ThemeError, "theme transition failed") as caught:
                        store.activate(candidate)
                self.assertEqual(
                    (store.current.read_bytes(), store.previous.read_bytes()), before
                )
                self.assertNotIn("private injected diagnostic", str(caught.exception))

    def test_rollback_restores_exact_pair_at_every_write_failure_boundary(self):
        store = ThemeStore(self.runtime_root)
        updated = self.valid.replace(b"#F6F7F9", b"#F5F6F8")
        store.activate(self.valid)
        store.activate(updated)
        before = (store.current.read_bytes(), store.previous.read_bytes())

        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                original = theme_module._atomic_write_at
                calls = 0

                def fail_once(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("private injected diagnostic")
                    return original(*args, **kwargs)

                with patch("local_web_server.theme._atomic_write_at", side_effect=fail_once):
                    with self.assertRaisesRegex(ThemeError, "theme transition failed"):
                        store.rollback()
                self.assertEqual(
                    (store.current.read_bytes(), store.previous.read_bytes()), before
                )

    def test_concurrent_activations_serialize_the_current_previous_pair(self):
        store = ThemeStore(self.runtime_root)
        store.activate(self.valid)
        first = self.valid.replace(b"#F6F7F9", b"#F5F6F8")
        second = self.valid.replace(b"#F6F7F9", b"#F4F5F7")
        barrier = threading.Barrier(2)

        def activate(content):
            barrier.wait()
            return store.activate(content)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(activate, (first, second)))

        self.assertEqual(len(set(results)), 2)
        self.assertEqual(
            {store.current.read_bytes(), store.previous.read_bytes()}, {first, second}
        )

    def test_refuses_missing_rollback_and_symlinked_or_non_regular_files(self):
        store = ThemeStore(self.runtime_root)
        with self.assertRaisesRegex(ThemeError, "rollback unavailable"):
            store.rollback()

        store.platform_root.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside.css"
        outside.write_bytes(self.valid)
        store.current.symlink_to(outside)
        with self.assertRaisesRegex(ThemeError, "managed theme file is unsafe"):
            store.activate(self.valid)
        self.assertEqual(outside.read_bytes(), self.valid)

        store.current.unlink()
        store.current.mkdir()
        with self.assertRaisesRegex(ThemeError, "managed theme file is unsafe"):
            store.activate(self.valid)

    def test_refuses_symlinked_platform_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.runtime_root.mkdir()
        (self.runtime_root / "platform").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ThemeError, "managed theme directory is unsafe"):
            ThemeStore(self.runtime_root).activate(self.valid)
        self.assertEqual(list(outside.iterdir()), [])

    def test_refuses_an_intermediate_symlink_without_writing_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ThemeError, "managed theme directory is unsafe"):
            ThemeStore(link / "runtime").activate(self.valid)
        self.assertEqual(list(outside.iterdir()), [])

    def test_platform_directory_swap_cannot_redirect_writes_and_restores_pinned_pair(self):
        store = ThemeStore(self.runtime_root)
        updated = self.valid.replace(b"#F6F7F9", b"#F5F6F8")
        candidate = self.valid.replace(b"#F6F7F9", b"#F4F5F7")
        store.activate(self.valid)
        store.activate(updated)
        before = (store.current.read_bytes(), store.previous.read_bytes())
        moved = self.runtime_root / "platform-pinned"
        outside = self.root / "outside-swap"
        outside.mkdir()
        original = theme_module._atomic_write_at
        swapped = False

        def swap_then_write(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                store.platform_root.rename(moved)
                store.platform_root.symlink_to(outside, target_is_directory=True)
            return original(*args, **kwargs)

        with patch("local_web_server.theme._atomic_write_at", side_effect=swap_then_write):
            with self.assertRaisesRegex(ThemeError, "theme transition failed"):
                store.activate(candidate)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(
            (
                moved.joinpath("theme.css").read_bytes(),
                moved.joinpath("theme.previous.css").read_bytes(),
            ),
            before,
        )


if __name__ == "__main__":
    unittest.main()
