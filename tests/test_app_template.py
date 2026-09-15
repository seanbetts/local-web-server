import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_web_server.app_template import TemplateError, TemplateInputs, render_template


EXPECTED_PATHS = {
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "docs/architecture.md",
    "eslint.config.js",
    "index.html",
    "local-web.json",
    "package.json",
    "playwright.config.ts",
    "tsconfig.app.json",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
    "src/App.tsx",
    "src/App.test.tsx",
    "src/contextExport.ts",
    "src/contextExport.test.ts",
    "src/app.css",
    "src/main.tsx",
    "src/platform.ts",
    "src/platform.test.ts",
    "src/test/setup.ts",
    "tests/app.spec.ts",
}


def recipe_inputs(**changes):
    values = {
        "app_id": "recipes",
        "title": "Recipe Collection",
        "route": "/recipes",
        "icon": "book",
        "accent": "#8EA7C6",
        "ui_version": "0.1.0",
        "ui_sha256": "a" * 64,
        "capabilities": (),
        "kind": "static",
    }
    values.update(changes)
    return TemplateInputs(**values)


class AppTemplateTests(unittest.TestCase):
    def rendered(self, **changes):
        return {
            item.path.as_posix(): item.content
            for item in render_template(recipe_inputs(**changes))
        }

    @staticmethod
    def isolated_git_environment():
        return {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def test_renders_the_complete_canonical_file_set(self):
        files = self.rendered()

        self.assertEqual(set(files), EXPECTED_PATHS)
        package = json.loads(files["package.json"])
        self.assertEqual(
            package["dependencies"]["@local-web/ui"],
            "file:vendor/local-web-ui.tgz",
        )
        self.assertNotRegex("".join(files.values()), r"__[A-Z0-9_]+__")
        self.assertTrue(all(content.endswith("\n") for content in files.values()))
        self.assertFalse(any("\r\n" in content for content in files.values()))

    def test_generated_checks_remain_app_local(self):
        package = json.loads(self.rendered()["package.json"])
        self.assertTrue({"check", "test:e2e"} <= package["scripts"].keys())
        self.assertFalse(any("local-web" in command for command in package["scripts"].values()))

    def test_configures_the_real_base_path_and_reserved_theme_route(self):
        files = self.rendered()
        index = files["index.html"]
        manifest = json.loads(files["local-web.json"])

        self.assertIn('href="/_local-web/platform/theme.css"', index)
        self.assertNotIn("local-web:colour-mode", index)
        self.assertNotIn("<script>", index)
        self.assertEqual(
            manifest["build"]["environment"],
            ["VITE_PUBLIC_BASE_PATH"],
        )

    def test_emits_valid_platform_metadata_and_agent_guidance(self):
        files = self.rendered(capabilities=("supabase",))
        manifest = json.loads(files["local-web.json"])

        self.assertEqual(manifest["id"], "recipes")
        self.assertEqual(manifest["route"], "/recipes")
        self.assertEqual(manifest["healthPath"], "/recipes/")
        self.assertEqual(manifest["home"], {"accent": "#8EA7C6", "icon": "book"})
        self.assertEqual(
            manifest["platform"],
            {
                "contractVersion": 1,
                "templateVersion": 3,
                "uiVersion": "0.1.0",
                "capabilities": ["supabase"],
            },
        )
        self.assertNotIn("API_KEY", "".join(files.values()))
        self.assertNotIn("SECRET", "".join(files.values()))

    def test_renders_an_explicit_service_foundation_with_declarative_release(self):
        files = self.rendered(kind="service")
        manifest = json.loads(files["local-web.json"])
        package = json.loads(files["package.json"])

        self.assertEqual(set(files), EXPECTED_PATHS | {"server/service.mjs"})
        self.assertEqual(manifest["kind"], "service")
        self.assertEqual(manifest["build"]["output"], "release")
        self.assertEqual(
            manifest["build"]["release"],
            [
                {"source": "dist", "target": "public"},
                {"source": "server", "target": "server"},
            ],
        )
        self.assertEqual(
            manifest["service"],
            {
                "module": "server/service.mjs",
                "internalHealthPath": "/healthz",
                "frontendOutput": "public",
                "proxyPaths": ["/api"],
                "startCommand": [
                    "/usr/bin/env",
                    "node",
                    "server/service.mjs",
                    "--port",
                    "{port}",
                ],
            },
        )
        self.assertEqual(manifest["healthPath"], "/recipes/healthz")
        self.assertEqual(package["scripts"]["test:service"], "node --check server/service.mjs")
        self.assertIn("npm run test:service", package["scripts"]["check"])
        self.assertNotIn("local-web", package["scripts"]["check"])

    def test_rejects_marker_syntax_and_noncanonical_values(self):
        cases = (
            {"title": "Bad __APP_TITLE__"},
            {"title": 'Bad "title"'},
            {"title": "Bad <title>"},
            {"app_id": "Recipes"},
            {"route": "/recipes/"},
            {"icon": "unknown"},
            {"accent": "#8ea7c6"},
            {"ui_version": "v0.1.0"},
            {"ui_sha256": "A" * 64},
            {"capabilities": ("supabase", "supabase")},
            {"capabilities": ["supabase"]},
            {"capabilities": None},
            {"capabilities": ("zebra",)},
            {"kind": "worker"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(TemplateError):
                    render_template(recipe_inputs(**changes))

    def test_ignores_typescript_incremental_build_metadata_in_a_rendered_app(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_directory = Path(temporary_directory)
            for path, content in self.rendered().items():
                destination = app_directory / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")

            git_environment = self.isolated_git_environment()
            subprocess.run(
                ("git", "init", "--quiet"),
                cwd=app_directory,
                env=git_environment,
                check=True,
            )
            subprocess.run(
                ("git", "add", "."),
                cwd=app_directory,
                env=git_environment,
                check=True,
            )
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Template Test",
                    "-c",
                    "user.email=template-test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "Rendered template",
                ),
                cwd=app_directory,
                env=git_environment,
                check=True,
            )
            for path in ("tsconfig.app.tsbuildinfo", "tsconfig.node.tsbuildinfo"):
                (app_directory / path).write_text('{"version":"6.0.3"}\n', encoding="utf-8")

            status = subprocess.run(
                ("git", "status", "--porcelain"),
                cwd=app_directory,
                env=git_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            for path in ("tsconfig.app.tsbuildinfo", "tsconfig.node.tsbuildinfo"):
                ignored = subprocess.run(
                    ("git", "check-ignore", "--verbose", "--", path),
                    cwd=app_directory,
                    env=git_environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertRegex(
                    ignored.stdout,
                    r"\.gitignore:\d+:\*\.tsbuildinfo\t",
                )


if __name__ == "__main__":
    unittest.main()
