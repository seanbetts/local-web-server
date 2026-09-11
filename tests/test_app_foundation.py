import json
import unittest
from dataclasses import replace
from pathlib import Path

from local_web_server.app_foundation import (
    render_generated_repository,
    render_react_vite_foundation,
)
from local_web_server.app_template import TemplateInputs, render_template


FRONTEND_PATHS = {
    Path("eslint.config.js"), Path("index.html"), Path("package.json"),
    Path("playwright.config.ts"), Path("tsconfig.app.json"),
    Path("tsconfig.json"), Path("tsconfig.node.json"), Path("vite.config.ts"),
    Path("src/App.tsx"), Path("src/App.test.tsx"), Path("src/app.css"),
    Path("src/contextExport.ts"), Path("src/contextExport.test.ts"),
    Path("src/main.tsx"), Path("src/platform.ts"),
    Path("src/platform.test.ts"), Path("src/test/setup.ts"),
    Path("tests/app.spec.ts"),
}


def inputs(kind: str = "static") -> TemplateInputs:
    return TemplateInputs(
        app_id="samplealpha-planning", title="Example Planning",
        route="/samplealpha-planning", icon="chart-line", accent="#74D3A4",
        ui_version="0.5.2", ui_sha256="a" * 64,
        capabilities=(), kind=kind,
    )


class AppFoundationTests(unittest.TestCase):
    def test_frontend_partition_has_the_exact_reviewed_target_set(self):
        result = render_react_vite_foundation(
            inputs("service"), include_reference_service=False
        )
        self.assertEqual({item.path for item in result.files}, FRONTEND_PATHS)
        self.assertEqual(result.agent_guidance.path, Path("AGENTS.md"))
        self.assertTrue(
            {Path("local-web.json"), Path("README.md"), Path(".gitignore"),
             Path("docs/architecture.md"), Path("server/service.mjs")}.isdisjoint(
                {item.path for item in result.files}
            )
        )

    def test_existing_service_adoption_gets_frontend_only_npm_checks(self):
        result = render_react_vite_foundation(
            inputs("service"), include_reference_service=False
        )
        package = json.loads(next(
            item.content for item in result.files if item.path == Path("package.json")
        ))
        self.assertEqual(
            package["scripts"]["check"],
            "npm run lint && npm run test && npm run build",
        )
        self.assertNotIn("test:service", package["scripts"])
        self.assertIn("App kind: `service`", result.agent_guidance.content)

    def test_generated_repository_is_byte_identical_to_the_existing_renderer(self):
        for kind in ("static", "service"):
            with self.subTest(kind=kind):
                candidate = inputs(kind)
                self.assertEqual(
                    render_generated_repository(candidate), render_template(candidate)
                )

    def test_foundation_renders_context_export_with_the_selected_frontend_kind(self):
        static = {
            item.path: item.content
            for item in render_react_vite_foundation(
                inputs("static"), include_reference_service=False
            ).files
        }
        service = {
            item.path: item.content
            for item in render_react_vite_foundation(
                inputs("service"), include_reference_service=True
            ).files
        }

        self.assertIn("appKind: 'static'", static[Path("src/contextExport.ts")])
        self.assertIn("appKind: 'service'", service[Path("src/contextExport.ts")])
