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
        self.assertIn(
            "<AppShell app={platformApp} buildContextExport={buildContextExport}>",
            files["src/App.tsx"],
        )
        self.assertIn("<ViewHeader", files["src/App.tsx"])
        self.assertIn('title="Overview"', files["src/App.tsx"])
        self.assertNotIn("<h1>Recipe Collection</h1>", files["src/App.tsx"])
        self.assertIn('"@local-web/ui": "file:vendor/local-web-ui.tgz"', files["package.json"])
        self.assertNotRegex("".join(files.values()), r"__[A-Z0-9_]+__")
        self.assertTrue(all(content.endswith("\n") for content in files.values()))
        self.assertFalse(any("\r\n" in content for content in files.values()))
        self.assertIn("margin: 0;", files["src/app.css"])

    def test_renders_a_complete_deterministic_context_export_adapter_for_each_app_kind(self):
        expected = {
            "static": {"route": "/recipes", "kind": "static"},
            "service": {"route": "/recipes", "kind": "service"},
        }

        for kind, values in expected.items():
            with self.subTest(kind=kind):
                files = self.rendered(kind=kind)
                adapter = files["src/contextExport.ts"]

                self.assertIn("signal.throwIfAborted();", adapter)
                self.assertIn("const generatedAt = new Date().toISOString();", adapter)
                self.assertIn("schema: 'local-web-context/v1'", adapter)
                self.assertIn("id: platformApp.id", adapter)
                self.assertIn("version: '0.0.0'", adapter)
                self.assertIn("sourceRevision: 'unknown'", adapter)
                self.assertIn(f"activeRoute: '{values['route']}'", adapter)
                self.assertIn("classification: 'private'", adapter)
                self.assertIn("Private Local Web context. Share deliberately.", adapter)
                self.assertIn("Generated application foundation", adapter)
                self.assertIn("Shared shell, theme, verification, and context export", adapter)
                self.assertIn("The generated foundation is connected and ready for app-owned features.", adapter)
                self.assertIn(f"appKind: '{values['kind']}'", adapter)
                self.assertIn("label: 'Generated application state'", adapter)
                self.assertIn("assumptions: []", adapter)
                self.assertIn("decisions: []", adapter)
                self.assertIn("Domain context has not been added yet.", adapter)
                self.assertIn("No whole-app source, runtime, or media is included.", adapter)
                self.assertIn("vi.setSystemTime", files["src/contextExport.test.ts"])
                self.assertIn("AbortSignal.abort()", files["src/contextExport.test.ts"])

    def test_pins_the_standard_toolchain_and_scripts_exactly(self):
        package = json.loads(self.rendered()["package.json"])

        self.assertEqual(
            package["scripts"],
            {
                "dev": "vite --host 127.0.0.1",
                "build": "tsc -b && vite build",
                "lint": "eslint .",
                "test": "vitest run",
                "test:e2e": "playwright test",
                "check": "npm run lint && npm run test && npm run build",
            },
        )
        self.assertFalse(any("local-web" in command for command in package["scripts"].values()))
        self.assertEqual(
            package["dependencies"],
            {
                "@local-web/ui": "file:vendor/local-web-ui.tgz",
                "react": "19.2.8",
                "react-dom": "19.2.8",
            },
        )
        self.assertEqual(
            package["devDependencies"],
            {
                "@eslint/js": "10.0.1",
                "@axe-core/playwright": "4.12.1",
                "@playwright/test": "1.62.1",
                "@testing-library/jest-dom": "7.0.0",
                "@testing-library/react": "16.3.2",
                "@types/node": "26.1.2",
                "@types/react": "19.2.18",
                "@types/react-dom": "19.2.4",
                "@vitejs/plugin-react": "6.0.5",
                "eslint": "10.8.0",
                "eslint-plugin-react-hooks": "7.1.1",
                "eslint-plugin-react-refresh": "0.5.3",
                "globals": "17.9.0",
                "jsdom": "30.0.1",
                "typescript": "6.0.3",
                "typescript-eslint": "8.66.0",
                "vite": "8.2.0",
                "vitest": "4.1.10",
            },
        )

    def test_configures_the_real_base_path_and_reserved_theme_route(self):
        files = self.rendered()
        index = files["index.html"]
        manifest = json.loads(files["local-web.json"])
        vite = files["vite.config.ts"]

        self.assertIn('href="/_local-web/platform/theme.css"', index)
        self.assertNotIn("local-web:colour-mode", index)
        self.assertNotIn("<script>", index)
        self.assertIn("process.env.VITE_PUBLIC_BASE_PATH ?? '/'", vite)
        self.assertIn("base: publicBasePath", vite)
        self.assertIn("localWebApp({", vite)
        self.assertIn("appId: 'recipes'", vite)
        self.assertIn("basePath: publicBasePath", vite)
        self.assertIn("'./src/**/*.test.ts'", vite)
        self.assertIn("'./src/**/*.test.tsx'", vite)
        self.assertIn("from './src/platform.ts'", vite)
        self.assertIn("from 'vitest/config'", vite)
        self.assertIn('"vite/client"', files["tsconfig.app.json"])
        self.assertIn('"src/platform.ts"', files["tsconfig.node.json"])
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
        self.assertIn("$local-web-app-development", files["AGENTS.md"])
        self.assertIn('"$LOCAL_WEB" app doctor', files["AGENTS.md"])
        self.assertIn('"$LOCAL_WEB" app doctor', files["README.md"])
        self.assertIn('"$LOCAL_WEB" app check', files["README.md"])
        self.assertNotIn("upgrade", files["AGENTS.md"].lower())
        self.assertIn("supabase", files["AGENTS.md"])
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
        self.assertIn("127.0.0.1", files["server/service.mjs"])
        self.assertIn(
            "ignores: ['coverage', 'dist', 'release']",
            files["eslint.config.js"],
        )

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

    def test_browser_smoke_uses_the_real_hosted_route(self):
        files = self.rendered()
        self.assertIn("page.goto('./')", files["tests/app.spec.ts"])
        self.assertIn("Export context", files["tests/app.spec.ts"])
        self.assertIn("suggestedFilename", files["tests/app.spec.ts"])
        self.assertIn("Content-Security-Policy", files["tests/app.spec.ts"])
        self.assertIn("data-context-export-classification>PRIVATE", files["tests/app.spec.ts"])
        self.assertIn(
            "This is a static snapshot. Changes do not sync to the source app.",
            files["tests/app.spec.ts"],
        )
        self.assertIn("process.env.VITE_PUBLIC_BASE_PATH ?? '/'", files["playwright.config.ts"])
        self.assertIn("normalizePublicBasePath('/')", files["src/platform.test.ts"])
        self.assertIn("normalizePublicBasePath('/recipes/')", files["src/platform.test.ts"])

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
