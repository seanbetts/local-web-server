import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_web_server.app_template import TemplateError, TemplateInputs, render_template
from local_web_server.config import parse_manifest
from local_web_server.nas_recipe import parse_production_recipe


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

    def test_static_nas_recipe_separates_native_dependencies_from_source_build(self):
        manifest = parse_manifest(self.rendered()['local-web.json'])
        self.assertEqual(manifest.production.data_contract, 'none')
        self.assertEqual(manifest.production.rollback_policy, 'compatible')
        self.assertIsNone(manifest.production.runtime)
        self.assertEqual([command.argv for command in manifest.production.build_commands], [('npm', 'run', 'build')])
        self.assertEqual([command.argv for command in manifest.production.native_dependencies.install_commands], [('npm', 'ci')])
        self.assertIn('package-lock.json', manifest.production.native_dependencies.lockfiles)
        self.assertIn('vendor/local-web-ui.tgz', manifest.production.native_dependencies.lockfiles)
        self.assertEqual(manifest.production.native_dependencies.outputs, ('node_modules',))

    def test_service_default_owns_pinned_builtin_runtime_and_accepts_explicit_override(self):
        generated = self.rendered(kind='service')
        production = parse_manifest(generated['local-web.json']).production
        self.assertEqual(production.data_contract, 'none')
        self.assertEqual(production.rollback_policy, 'compatible')
        self.assertEqual(production.runtime.health_runtime, 'node')
        self.assertEqual(production.runtime.lockfiles, ('runtime/dependencies.json',))
        self.assertTrue(production.runtime.base_image.startswith('node@sha256:'))
        self.assertIn('FROM ${RUNTIME_IMAGE}', generated['runtime/Dockerfile'])
        self.assertNotIn('RUN ', generated['runtime/Dockerfile'])
        self.assertEqual(production.runtime.command[0:2], ('node', '/app/current/server/service.mjs'))
        self.assertIn('{publicOrigin}', production.runtime.command)
        self.assertEqual(production.native_dependencies.outputs, ('node_modules',))
        value = dict(recipeVersion='1', dataContract='none', rollbackPolicy='compatible',
                     buildCommands=[['npm', 'run', 'build']], nativeDependencies=None,
                     runtime=dict(baseImage='node@sha256:' + 'a' * 64, abi='node-24-linux-amd64', healthRuntime='node',
                         lockfiles=['package-lock.json'], dockerfile='Dockerfile.runtime',
                         command=['node', 'server/service.mjs', '--port', '{port}', '--bind-host', '{bindHost}'],
                         cpuLimit=1, memoryLimitMib=512, secretFiles=[]))
        recipe = parse_production_recipe(value, kind='service')
        manifest = parse_manifest(self.rendered(kind='service', production_recipe=recipe)['local-web.json'])
        self.assertEqual(manifest.production, recipe)
        with self.assertRaises(TemplateError):
            self.rendered(kind='static', production_recipe=recipe)

    def test_service_listener_defaults_to_loopback_and_accepts_explicit_container_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'service.mjs'
            path.write_text(self.rendered(kind='service')['server/service.mjs'])
            for argument, expected in ((None, '127.0.0.1'), ('0.0.0.0', '0.0.0.0')):
                argv = ['node', str(path), '--port', '12345']
                if argument is not None:
                    argv += ['--runtime', 'nas-docker', '--bind-host', argument, '--public-origin', 'https://local.example.ts.net', '--proxy-address', '172.30.200.2']
                script = ("import { Server } from 'node:http'; "
                          "Server.prototype.listen = function(port, host) { console.log(JSON.stringify({port,host})); return this; }; "
                          "process.argv = " + json.dumps(argv) + "; await import(" + json.dumps(path.as_uri()) + ");")
                result = subprocess.run(('node', '--input-type=module', '-e', script), check=True,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(json.loads(result.stdout), dict(port=12345, host=expected))

    def test_service_nas_request_trust_uses_socket_peer_host_and_canonical_origin(self):
        headers = {'host':'local.example.ts.net', 'x-forwarded-proto':'https'}
        cases = [
            ('172.30.200.2', '/api/hello', headers, 200),
            ('::ffff:172.30.200.2', '/api/hello', dict(headers, origin='https://local.example.ts.net'), 200),
            ('172.30.200.3', '/api/hello', headers, 403),
            ('172.30.200.2', '/api/hello', dict(headers, host='other.example.test'), 403),
            ('172.30.200.2', '/api/hello', {'host':'local.example.ts.net'}, 403),
            ('172.30.200.2', '/api/hello', dict(headers, **{'x-forwarded-host':'other.example.test'}), 403),
            ('172.30.200.2', '/api/hello', dict(headers, origin='https://other.example.test'), 403),
            ('127.0.0.1', '/healthz', {'host':'127.0.0.1:12345'}, 204),
            ('172.30.200.3', '/healthz', {'host':'127.0.0.1:12345'}, 403),
            ('172.30.200.2', '/api/hello', headers, 403, ['Host','local.example.ts.net','Host','local.example.ts.net','X-Forwarded-Proto','https']),
            ('172.30.200.2', '/api/hello', dict(headers, forwarded='proto=https'), 403),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'service.mjs'
            path.write_text(self.rendered(kind='service')['server/service.mjs'])
            argv = ['node', str(path), '--runtime', 'nas-docker', '--port', '12345', '--bind-host', '0.0.0.0',
                    '--public-origin', 'https://local.example.ts.net', '--proxy-address', '172.30.200.2']
            script = ("import { Server } from 'node:http'; const cases="+json.dumps(cases)+"; const results=[]; "
                "Server.prototype.listen=function(){ const handler=this.listeners('request')[0]; for(const [peer,url,headers,,rawHeaders] of cases){ "
                "const request={url,method:'GET',headers,rawHeaders:rawHeaders??Object.entries(headers).flat(),socket:{remoteAddress:peer}}; "
                "const response={writeHead(status){this.status=status;return this;},end(){results.push(this.status);}}; handler(request,response); "
                "} console.log(JSON.stringify(results)); return this; }; process.argv="+json.dumps(argv)+"; await import("+json.dumps(path.as_uri())+");")
            result=subprocess.run(('node','--input-type=module','-e',script),capture_output=True,text=True,timeout=5,check=True)
            self.assertEqual(json.loads(result.stdout), [case[3] for case in cases])

    def test_app_updater_preserves_app_owned_production_recipe(self):
        from local_web_server.app_update import _manifest_candidate
        manifest = json.loads(self.rendered(kind='service')['local-web.json'])
        manifest['production']['recipeVersion'] = 'app-owned-2'
        manifest['production']['dataContract'] = 'recipes-v2'
        content, _, _, _ = _manifest_candidate(json.dumps(manifest).encode(), '0.8.0', ())
        self.assertEqual(json.loads(content)['production'], manifest['production'])

    def test_service_rejects_implicit_exposure_unknown_flags_and_noncanonical_nas_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'service.mjs'
            path.write_text(self.rendered(kind='service')['server/service.mjs'])
            valid = ['--runtime','nas-docker','--bind-host','0.0.0.0','--public-origin','https://local.example.ts.net','--proxy-address','172.30.200.2']
            cases = [['--bind-host','0.0.0.0'], ['--unknown','value'], ['--port','12345'],
                     valid[:], valid[:], valid[:]]
            cases[3][cases[3].index('https://local.example.ts.net')] = 'http://local.example.ts.net'
            cases[4][cases[4].index('https://local.example.ts.net')] = 'https://local.example.ts.net/'
            cases[5][cases[5].index('172.30.200.2')] = '203.0.113.2'
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(('node',str(path),'--port','12345',*arguments),capture_output=True,timeout=5)
                    self.assertNotEqual(result.returncode, 0)

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

        self.assertEqual(set(files), EXPECTED_PATHS | {"server/service.mjs", "runtime/Dockerfile", "runtime/dependencies.json"})
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
