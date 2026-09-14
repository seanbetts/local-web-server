import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_web_server import public_release
from local_web_server.config import load_registry
from local_web_server.public_release import (
    PublicReleaseError,
    load_private_policy,
    verify_public_history,
    verify_public_tree,
)


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/verify_public_release.py"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"


def _load_ci_workflow() -> dict[str, object]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate workflow key: {key}")
            result[key] = value
        return result

    document = json.loads(
        CI_WORKFLOW.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise ValueError("CI workflow must be a mapping")
    return document


def _workflow_runs(job: dict[str, object]) -> tuple[str, ...]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise ValueError("CI job steps must be a list")
    return tuple(
        step["run"]
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )


def _git_environment(**overrides: str) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            **overrides,
        }
    )
    return environment


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        (
            "git",
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ),
        cwd=repository,
        check=check,
        capture_output=True,
        env=_git_environment(),
        text=True,
    )


def _safe_files() -> dict[str, bytes]:
    example_registry = {
        "schemaVersion": 1,
        "host": "example-mac.local",
        "runtimeRoot": "/Users/example/Library/Application Support/LocalWebServer",
        "apps": [
            {
                "id": "example-static",
                "repository": "/Users/example/Coding/example-static",
                "autoDeploy": True,
                "environmentFile": "/Users/example/Coding/example-static/.env",
                "environment": {"VITE_PUBLIC_BASE_PATH": "/example-static/"},
            },
            {
                "id": "example-service",
                "repository": "/Users/example/Coding/example-service",
                "autoDeploy": True,
                "port": 9000,
                "startCommand": [
                    "/opt/homebrew/bin/python3",
                    "-m",
                    "example_service",
                    "--port",
                    "9000",
                ],
            },
        ],
    }
    return {
        ".gitignore": b"node_modules/\n",
        "README.md": (
            b"# Local Web Server\n\n"
            b"## Architecture\n\nImmutable releases and a private host profile.\n\n"
            b"## Screenshots\n\n"
            b"![Generic index](apps/system-index/tests/snapshots/index-light-desktop.png)\n\n"
            b"## Prerequisites\n\nPython, Node, Caddy, and macOS.\n\n"
            b"## Bootstrap a host\n\nUse `local-web host init`.\n\n"
            b"```sh\npython3 scripts/install_local_web.py --dry-run\n```\n\n"
            b"After approval, apply with:\n\n"
            b"```sh\npython3 scripts/install_local_web.py\n```\n\n"
            b"## Create an application\n\nUse disposable-first app tooling.\n\n"
            b"## Security model\n\nChoose trusted LAN or Tailscale Serve deliberately.\n\n"
            b"## Verification\n\nRun `npm run verify:public-release`.\n\n"
            b"## Case studies\n\nSanitised product-level examples only.\n\n"
            b"## Limitations\n\nThis is a single-user macOS host framework.\n\n"
            b"Interactive export keeps snapshotData and viewState separate.\n"
            b"Examples use /Users/example/Coding, local.example.ts.net, "
            b"and fictional ports 48123 and 48234.\n"
        ),
        "LICENSE": (
            b"MIT License\n\nCopyright (c) 2026 Example Contributor\n\n"
            b"Permission is hereby granted, free of charge, to any person obtaining a copy\n"
            b"THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND.\n"
        ),
        "SECURITY.md": (
            b"# Security\n\nUse GitHub private vulnerability reporting. "
            b"Do not post secrets in public issues.\n"
        ),
        "CONTRIBUTING.md": (
            b"# Contributing\n\nUse disposable fixtures. Install Python, Node, Caddy, and npm.\n\n"
            b"`python3 -m unittest discover -s tests -v`\n\n"
            b"`npm run verify:host-profile`\n\n"
            b"`npm run verify:public-release`\n\n"
            b"`npm audit --omit=dev`\n"
        ),
        "package.json": b'{"name":"local-web-server","private":true}\n',
        "config/apps.example.json": (
            json.dumps(example_registry, sort_keys=True).encode("utf-8") + b"\n"
        ),
        "config/local/.gitignore": b"*\n!.gitignore\n!README.md\n",
        "config/local/README.md": (
            b"# Private host state\n\nNever commit local state. Preserve `apps.json`, `history/`, "
            b"`.host-profile-transaction.json`, `backups/`, `publication-policy.json`, "
            b"the private `.bundle`, and its `.sha256` with machine backups. "
            b"Directories use 0700 and files use 0600.\n"
        ),
        "docs/architecture.md": (
            b"# Architecture\n\nGeneric framework boundaries use immutable releases, "
            b"HostProfileStore, `trusted-lan`, and `tailscale-serve` with "
            b"Caddy on `127.0.0.1:8080`.\n"
        ),
        "docs/operations/host-profile.md": (
            b"# Host profile\n\n"
            b"`local-web host init`\n`local-web host migrate-registry`\n"
            b"`local-web host status`\n`local-web host backup`\n"
            b"`local-web host restore`\n`local-web host recover`\n\n"
            b"Private `.host-profile-recovery` audit residue is excluded from revisions "
            b"and backups, is bounded to 128 batches and 256 MiB, has no automatic "
            b"pruning, and fails closed pending deliberate operator handling.\n"
        ),
        "docs/operations/recovery.md": (
            b"# Recovery\n\nUse `local-web host status`, `local-web host recover`, "
            b"`local-web host restore`, `local-web rollback`, and "
            b"`python3 scripts/install_local_web.py --dry-run`.\n\n"
            b"```sh\npython3 scripts/install_local_web.py --dry-run\n```\n\n"
            b"After approval, apply with:\n\n"
            b"```sh\npython3 scripts/install_local_web.py\n```\n"
        ),
        "local_web_server/example.py": b'"""Small generic fixture."""\n',
    }


class DisposableRepository:
    def __init__(self, root: Path, name: str = "repository"):
        self.path = root / name
        self.path.mkdir()
        _git(self.path, "init", "--initial-branch=main")
        _git(self.path, "config", "user.name", "Public Release Tests")
        _git(self.path, "config", "user.email", "tests@example.invalid")
        for relative, content in _safe_files().items():
            self.write(relative, content)
        _git(self.path, "add", "--all")
        self.commit("safe public fixture")

    def write(self, relative: str, content: bytes | str) -> Path:
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path

    def commit(self, message: str) -> str:
        _git(self.path, "add", "--all")
        _git(self.path, "commit", "-m", message)
        return self.head()

    def head(self) -> str:
        return _git(self.path, "rev-parse", "HEAD").stdout.strip()

    def remove_and_commit(self, relative: str) -> None:
        _git(self.path, "rm", "--", relative)
        self.commit(f"remove {relative}")


class PublicReleaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = DisposableRepository(self.root)

    def assert_rule(self, expected: str, action) -> PublicReleaseError:
        with self.assertRaises(PublicReleaseError) as raised:
            action()
        error = raised.exception
        self.assertEqual(error.rule, expected)
        self.assertLessEqual(len(str(error)), 320)
        self.assertNotIn(str(self.root), str(error))
        return error

    def write_private_policy(self, payload: object, relative: str = "config/local/publication-policy.json") -> Path:
        path = self.repository.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        path.parent.chmod(0o700)
        return path


class PrivatePublicationPolicyTests(PublicReleaseTestCase):
    def test_loads_exact_schema_and_unique_nonempty_deny_strings(self):
        first = "host-" + "private.example"
        second = "/Volumes/" + "private-data"
        path = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": [first, second],
            }
        )

        policy = load_private_policy(self.repository.path, path)

        self.assertEqual(policy.deny_strings, (first, second))
        self.assertNotIn(first, repr(policy))
        self.assertNotIn(second, repr(policy))

    def test_rejects_unknown_or_missing_policy_keys_without_disclosure(self):
        private_value = "do-not-" + "print-this-value"
        cases = (
            {"schema": "local-web-publication-policy/v1"},
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": [private_value],
                "comment": private_value,
            },
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                path = self.write_private_policy(payload)
                error = self.assert_rule(
                    "policy.schema",
                    lambda: load_private_policy(self.repository.path, path),
                )
                self.assertNotIn(private_value, str(error))

    def test_rejects_wrong_schema_malformed_json_and_duplicate_json_keys(self):
        cases = (
            b'{"schema":"other","denyStrings":["value"]}',
            b'{"schema":',
            (
                b'{"schema":"local-web-publication-policy/v1",'
                b'"denyStrings":["value"],"denyStrings":["other"]}'
            ),
            b"[" * 2000 + b"0" + b"]" * 2000,
        )
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                path = self.repository.path / "config/local/publication-policy.json"
                path.write_bytes(content)
                path.chmod(0o600)
                path.parent.chmod(0o700)
                self.assert_rule(
                    "policy.schema",
                    lambda: load_private_policy(self.repository.path, path),
                )

    def test_rejects_empty_duplicate_nonstring_control_and_noncanonical_values(self):
        cases = (
            [],
            [""],
            ["   "],
            ["same", "same"],
            ["valid", 7],
            ["line\nbreak"],
            ["e\N{COMBINING ACUTE ACCENT}"],
        )
        for index, values in enumerate(cases):
            with self.subTest(index=index):
                path = self.write_private_policy(
                    {
                        "schema": "local-web-publication-policy/v1",
                        "denyStrings": values,
                    }
                )
                self.assert_rule(
                    "policy.values",
                    lambda: load_private_policy(self.repository.path, path),
                )

    def test_rejects_policy_value_count_value_size_and_document_size_bounds(self):
        cases = (
            [f"value-{index}" for index in range(257)],
            ["x" * 4097],
        )
        for index, values in enumerate(cases):
            with self.subTest(index=index):
                path = self.write_private_policy(
                    {
                        "schema": "local-web-publication-policy/v1",
                        "denyStrings": values,
                    }
                )
                self.assert_rule(
                    "policy.bounds",
                    lambda: load_private_policy(self.repository.path, path),
                )

        path = self.repository.path / "config/local/publication-policy.json"
        path.write_bytes(b" " * (64 * 1024 + 1))
        path.chmod(0o600)
        path.parent.chmod(0o700)
        self.assert_rule(
            "policy.bounds",
            lambda: load_private_policy(self.repository.path, path),
        )

    def test_rejects_symlinked_permissive_unignored_external_and_tracked_policies(self):
        payload = {
            "schema": "local-web-publication-policy/v1",
            "denyStrings": ["private-value"],
        }
        real = self.write_private_policy(payload)

        real.chmod(0o644)
        self.assert_rule(
            "policy.permissions",
            lambda: load_private_policy(self.repository.path, real),
        )

        real.chmod(0o600)
        replacement = real.with_name("policy-target.json")
        real.rename(replacement)
        real.symlink_to(replacement.name)
        self.assert_rule(
            "policy.path",
            lambda: load_private_policy(self.repository.path, real),
        )
        real.unlink()
        replacement.rename(real)

        unignored = self.repository.path / "policy.json"
        unignored.write_text(json.dumps(payload), encoding="utf-8")
        unignored.chmod(0o600)
        self.assert_rule(
            "policy.ignored",
            lambda: load_private_policy(self.repository.path, unignored),
        )

        external = self.root / "external-policy.json"
        external.write_text(json.dumps(payload), encoding="utf-8")
        external.chmod(0o600)
        self.assert_rule(
            "policy.path",
            lambda: load_private_policy(self.repository.path, external),
        )

        _git(self.repository.path, "add", "--force", "--", "config/local/publication-policy.json")
        self.assert_rule(
            "policy.tracked",
            lambda: load_private_policy(self.repository.path, real),
        )
        self.repository.commit("track forbidden private policy")
        self.assert_rule(
            "policy.tracked",
            lambda: load_private_policy(self.repository.path, real),
        )

    def test_rejects_policy_beneath_symlinked_parent(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        target = outside / "publication-policy.json"
        target.write_text(
            json.dumps(
                {
                    "schema": "local-web-publication-policy/v1",
                    "denyStrings": ["private-value"],
                }
            ),
            encoding="utf-8",
        )
        target.chmod(0o600)
        private = self.repository.path / "private"
        private.symlink_to(outside, target_is_directory=True)
        (self.repository.path / ".gitignore").write_text(
            "node_modules/\nprivate/\n", encoding="utf-8"
        )
        self.repository.commit("ignore private directory")

        self.assert_rule(
            "policy.path",
            lambda: load_private_policy(
                self.repository.path, private / "publication-policy.json"
            ),
        )

    def test_rejects_internal_symlink_alias_without_normalising_it_away(self):
        policy = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": ["private-value"],
            }
        )
        alias = self.repository.path / "private-alias"
        alias.symlink_to("config/local", target_is_directory=True)
        self.repository.write(".gitignore", "node_modules/\nprivate-alias/\n")
        self.repository.commit("ignore lexical private alias")

        self.assert_rule(
            "policy.path",
            lambda: load_private_policy(self.repository.path, alias / policy.name),
        )

    def test_rejects_policy_tracked_in_head_even_when_removed_from_index(self):
        policy = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": ["private-value"],
            }
        )
        _git(self.repository.path, "add", "--force", "--", "config/local/publication-policy.json")
        self.repository.commit("track forbidden private policy")
        _git(self.repository.path, "rm", "--cached", "--", "config/local/publication-policy.json")

        self.assertTrue(policy.exists())
        self.assert_rule(
            "policy.tracked",
            lambda: load_private_policy(self.repository.path, policy),
        )


class PublicTrackedTreeTests(PublicReleaseTestCase):
    def test_small_generic_fixture_and_documented_placeholders_pass(self):
        verify_public_tree(self.repository.path)

        payload = json.loads(
            self.repository.path.joinpath("config/apps.example.json").read_text(
                encoding="utf-8"
            )
        )
        payload.pop("host")
        payload.update(
            schemaVersion=2,
            publicOrigin="https://local.example.ts.net",
            ingressMode="tailscale-serve",
            runtimeRoot="/Users/example/Coding/runtime",
        )
        self.repository.write("config/apps.example.json", json.dumps(payload) + "\n")
        self.repository.commit("use generic version two host example")
        verify_public_tree(self.repository.path)

    def test_rejects_staged_tree_ambiguity(self):
        self.repository.write("README.md", "# Local Web Server\n\nStaged but uncommitted.\n")
        _git(self.repository.path, "add", "--", "README.md")

        self.assert_rule(
            "tree.staged", lambda: verify_public_tree(self.repository.path)
        )

    def test_object_inspection_uses_two_batches_and_one_operation_deadline(self):
        original = public_release._run_git
        calls: list[tuple[tuple[str, ...], float | None]] = []

        def recording(repository, arguments, **options):
            calls.append((arguments, options.get("deadline")))
            return original(repository, arguments, **options)

        with mock.patch.object(public_release, "_run_git", side_effect=recording):
            verify_public_tree(self.repository.path)

        deadlines = {deadline for _, deadline in calls}
        self.assertEqual(len(deadlines), 1)
        self.assertNotIn(None, deadlines)
        self.assertEqual(
            [arguments for arguments, _ in calls if arguments[:1] == ("cat-file",)],
            [("cat-file", "--batch-check"), ("cat-file", "--batch")],
        )

    def test_requires_each_public_contract_file(self):
        required = (
            "README.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "package.json",
            "config/apps.example.json",
            "config/local/.gitignore",
            "config/local/README.md",
            "docs/architecture.md",
            "docs/operations/host-profile.md",
            "docs/operations/recovery.md",
        )
        for index, relative in enumerate(required):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.remove_and_commit(relative)
                error = self.assert_rule(
                    "tree.required",
                    lambda: verify_public_tree(repository.path),
                )
                self.assertEqual(error.relative_path, relative)

    def test_requires_package_to_remain_private(self):
        for payload in ({"name": "local-web-server"}, {"name": "local-web-server", "private": False}, []):
            with self.subTest(payload=payload):
                self.repository.write("package.json", json.dumps(payload))
                self.repository.commit("make package publishable")
                self.assert_rule(
                    "package.private",
                    lambda: verify_public_tree(self.repository.path),
                )

    def test_requires_a_structural_generic_example_registry_and_mit_license(self):
        invalid_examples = (
            b"not json\n",
            b"[]\n",
            b'{"schemaVersion":2}\n',
            b'{"schemaVersion":7,"apps":[]}\n',
        )
        for index, content in enumerate(invalid_examples):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write("config/apps.example.json", content)
                repository.commit("break example registry")
                self.assert_rule(
                    "tree.example-registry",
                    lambda: verify_public_tree(repository.path),
                )

        self.repository.write("LICENSE", "A different license\n")
        self.repository.commit("replace license")
        self.assert_rule(
            "tree.license", lambda: verify_public_tree(self.repository.path)
        )

    def test_requires_exact_private_ignore_and_public_document_contracts(self):
        cases = (
            ("config/local/.gitignore", b"*\nREADME.md\n", "tree.private-ignore"),
            ("config/local/README.md", b"# Private host state\n", "tree.documentation"),
            ("README.md", b"# Local Web Server\n", "tree.documentation"),
            ("SECURITY.md", b"# Security\n", "tree.documentation"),
            ("CONTRIBUTING.md", b"# Contributing\n", "tree.documentation"),
            ("docs/architecture.md", b"# Architecture\n", "tree.documentation"),
            ("docs/operations/host-profile.md", b"# Host profile\n", "tree.documentation"),
            ("docs/operations/recovery.md", b"# Recovery\n", "tree.documentation"),
        )
        for index, (relative, content, rule) in enumerate(cases):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(relative, content)
                repository.commit("break public contract")
                self.assert_rule(rule, lambda: verify_public_tree(repository.path))

    def test_rejects_links_to_private_planning_material_in_public_docs(self):
        for index, target in enumerate((".superpowers/report.md", "docs/superpowers/plan.md")):
            with self.subTest(target=target), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(
                    "README.md",
                    _safe_files()["README.md"] + f"\n[private plan]({target})\n".encode(),
                )
                repository.commit("link private planning material")
                self.assert_rule(
                    "tree.documentation", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_stale_registry_git_wording_in_public_guidance(self):
        phrases = (
            "activation makes a registry commit",
            "activation makes a platform-registry commit",
            "activation committed registry state",
            "activation will commit the registry",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write("docs/activation.md", phrase + "\n")
                repository.commit("add stale registry guidance")
                self.assert_rule(
                    "tree.documentation", lambda: verify_public_tree(repository.path)
                )

    def test_requires_generic_content_in_both_approved_registry_fixtures(self):
        generic_index = {
            "schemaVersion": 1,
            "apps": [
                {
                    "id": "example-static",
                    "title": "Example Static",
                    "route": "/example-static/",
                    "icon": "file-description",
                    "accent": "#6688AA",
                    "frontendHealthPath": "/example-static/",
                    "backendHealthPath": None,
                }
            ],
        }
        self.repository.write(
            "apps/system-index/fixtures/registry-v1.json",
            json.dumps(generic_index, sort_keys=True) + "\n",
        )
        self.repository.commit("add generic index fixture")
        verify_public_tree(self.repository.path)

        generic_index["apps"][0]["id"] = "personal-app"
        self.repository.write(
            "apps/system-index/fixtures/registry-v1.json",
            json.dumps(generic_index, sort_keys=True) + "\n",
        )
        self.repository.commit("make index fixture specific")
        self.assert_rule(
            "content.inventory", lambda: verify_public_tree(self.repository.path)
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "specific-config-example")
            payload = json.loads(
                repository.path.joinpath("config/apps.example.json").read_text(
                    encoding="utf-8"
                )
            )
            payload["apps"][0]["id"] = "personal-app"
            repository.write("config/apps.example.json", json.dumps(payload) + "\n")
            repository.commit("make config example specific")
            self.assert_rule(
                "tree.example-registry", lambda: verify_public_tree(repository.path)
            )

    def test_environment_files_allow_only_documented_placeholder_locations(self):
        verify_public_tree(self.repository.path)

        payload = json.loads(
            self.repository.path.joinpath("config/apps.example.json").read_text(
                encoding="utf-8"
            )
        )
        payload["apps"][0]["environmentFile"] = (
            "/Users/example/Library/Application Support/LocalWebServer/"
            "runtime/example-static/.env"
        )
        self.repository.write("config/apps.example.json", json.dumps(payload) + "\n")
        self.repository.commit("point example at runtime environment")
        self.assert_rule(
            "content.environment-file", lambda: verify_public_tree(self.repository.path)
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "environment-doc")
            unsafe_paths = (
                "/srv/local-web/" + "private.env",
                "/srv/local-web/" + ".env.production",
            )
            for index, private_environment in enumerate(unsafe_paths):
                with self.subTest(index=index):
                    repository.write(
                        "docs/architecture.md",
                        f"Load {private_environment} at runtime.\n",
                    )
                    repository.commit("document private environment location")
                    self.assert_rule(
                        "content.environment-file",
                        lambda: verify_public_tree(repository.path),
                    )

    def test_rejects_private_and_planning_paths(self):
        cases = (
            ("config/apps.json", "tree.private-path"),
            ("config/local/apps.json", "tree.private-path"),
            ("config/local/history/revision.json", "tree.private-path"),
            (".superpowers/report.md", "tree.private-path"),
            ("docs/superpowers/plan.md", "tree.private-path"),
        )
        for index, (relative, rule) in enumerate(cases):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(relative, b"private fixture\n")
                _git(repository.path, "add", "--force", "--", relative)
                repository.commit("add forbidden path")
                self.assert_rule(rule, lambda: verify_public_tree(repository.path))

    def test_rejects_unapproved_top_level_and_config_paths(self):
        cases = ("notes/readme.md", "config/another-example.json", "random.txt")
        for index, relative in enumerate(cases):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(relative, b"unexpected\n")
                repository.commit("add unexpected path")
                self.assert_rule(
                    "path.allowlist", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_credential_shaped_filenames(self):
        names = (
            ".en" + "v",
            "config/creden" + "tials.json",
            "docs/id_" + "rsa",
            "scripts/client-" + "secret.txt",
            "docs/certificate." + "pem",
        )
        for index, relative in enumerate(names):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(relative, b"placeholder\n")
                repository.commit("add credential-shaped path")
                self.assert_rule(
                    "path.credential", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_tracked_runtime_build_and_test_output(self):
        paths = (
            "artifacts/release.tgz",
            "apps/example/dist/app.js",
            "examples/example/test-results/result.json",
            "local_web_server/__pycache__/module.pyc",
            "coverage/report.json",
            "packages/ui/cache.tsbuildinfo",
        )
        for index, relative in enumerate(paths):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write(relative, b"generated output\n")
                repository.commit("add generated output")
                self.assert_rule(
                    "path.generated", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_tracked_symlinks_and_gitlinks(self):
        link = self.repository.path / "docs/link.md"
        link.symlink_to("architecture.md")
        self.repository.commit("add tracked symlink")
        self.assert_rule(
            "path.file-type", lambda: verify_public_tree(self.repository.path)
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "gitlink-case")
            object_id = repository.head()
            _git(
                repository.path,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{object_id},apps/nested",
            )
            _git(repository.path, "commit", "-m", "add gitlink")
            self.assert_rule(
                "path.file-type", lambda: verify_public_tree(repository.path)
            )

    def test_rejects_fifo_socket_and_device_worktree_types(self):
        relative = "docs/architecture.md"
        path = self.repository.path / relative
        path.unlink()
        os.mkfifo(path)
        self.assert_rule(
            "path.file-type", lambda: verify_public_tree(self.repository.path)
        )
        path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(path))
        self.assert_rule(
            "path.file-type", lambda: verify_public_tree(self.repository.path)
        )
        listener.close()
        path.unlink()

        self.assertFalse(public_release.is_safe_worktree_mode(stat.S_IFCHR | 0o600))
        self.assertFalse(public_release.is_safe_worktree_mode(stat.S_IFBLK | 0o600))

    def test_rejects_private_key_and_high_confidence_credential_content(self):
        markers = (
            "-----BEGIN " + "PRIVATE KEY-----\nfixture\n",
            "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----\nfixture\n",
            "AK" + "IA" + "A" * 16,
            "gh" + "p_" + "a" * 36,
            "postgresql://user:" + "password@db.example.invalid/data",
        )
        for index, marker in enumerate(markers):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write("docs/architecture.md", marker)
                repository.commit("add unsafe content")
                self.assert_rule(
                    "content.credential", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_non_text_and_control_bearing_blobs_outside_assets(self):
        cases = (
            b"plain\x00hidden\n",
            b"not-utf8-\xff\n",
        )
        for index, content in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                repository.write("docs/architecture.md", content)
                repository.commit("add non-text content")
                self.assert_rule(
                    "content.binary", lambda: verify_public_tree(repository.path)
                )

    def test_rejects_personal_paths_hosts_tailnets_databases_and_inventory(self):
        cases = (
            ("/Users/" + "actual-user/Coding/app\n", "content.personal-path"),
            ("/home/" + "actual-user/projects/app\n", "content.personal-path"),
            ("/Volumes/" + "Personal/data/app.sqlite\n", "content.database"),
            ("Owner-" + "Mac-mini\n", "content.host"),
            (
                "https://host." + "private-tailnet" + ".ts." + "net\n",
                "content.tailnet",
            ),
            ("DATABASE_" + "URL=sqlite:////srv/private.sqlite\n", "content.database"),
            (
                json.dumps({"schemaVersion": 2, "apps": [{"id": "complete"}]}),
                "content.inventory",
            ),
        )
        for index, (content, rule) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                dir=self.root
            ) as temporary:
                repository = DisposableRepository(Path(temporary), f"case-{index}")
                relative = (
                    "docs/private-inventory.json"
                    if rule == "content.inventory"
                    else "docs/architecture.md"
                )
                repository.write(relative, content)
                repository.commit("add private-shaped content")
                self.assert_rule(rule, lambda: verify_public_tree(repository.path))

    def test_allows_only_fixed_placeholder_forms(self):
        content = (
            "Use /Users/example/Coding/demo, /home/example/demo, and "
            "https://local.example.ts.net with ports 43123 and 49151.\n"
        )
        self.repository.write("docs/placeholders.md", content)
        self.repository.commit("document placeholders")

        verify_public_tree(self.repository.path)

    def test_rejects_unexpected_large_blobs_but_allows_bounded_approved_pngs(self):
        self.repository.write(
            "docs/architecture.md", b"x" * (public_release.MAX_TEXT_FILE_BYTES + 1)
        )
        self.repository.commit("add oversized text")
        self.assert_rule(
            "content.size", lambda: verify_public_tree(self.repository.path)
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "approved-asset")
            asset_relative = "apps/system-index/tests/snapshots/index-dark-desktop.png"
            png = ROOT.joinpath(asset_relative).read_bytes()
            repository.write(
                asset_relative, png
            )
            repository.commit("add approved screenshot")
            verify_public_tree(repository.path)

            repository.write(
                asset_relative,
                b"\x89PNG\r\n\x1a\n"
                + b"\0" * public_release.MAX_APPROVED_ASSET_BYTES,
            )
            repository.commit("oversize approved screenshot")
            self.assert_rule(
                "content.size", lambda: verify_public_tree(repository.path)
            )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "invalid-asset")
            repository.write(
                "apps/system-index/tests/snapshots/index-dark-desktop.png",
                b"not a png",
            )
            repository.commit("add invalid approved asset")
            self.assert_rule(
                "content.asset", lambda: verify_public_tree(repository.path)
            )

    def test_assets_are_digest_pinned_and_scanned_for_credentials_first(self):
        relative = "apps/system-index/tests/snapshots/index-dark-desktop.png"
        approved = ROOT.joinpath(relative).read_bytes()
        for reviewed_path, reviewed_digest in sorted(
            public_release._APPROVED_ASSET_DIGESTS.items()
        ):
            reviewed = ROOT.joinpath(reviewed_path).read_bytes()
            self.assertEqual(hashlib.sha256(reviewed).hexdigest(), reviewed_digest)
            self.assertIsNone(public_release._raw_credential_rule(reviewed))

        self.repository.write(relative, approved[:-1] + bytes([approved[-1] ^ 1]))
        self.repository.commit("tamper approved screenshot")
        self.assert_rule(
            "content.asset-digest", lambda: verify_public_tree(self.repository.path)
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "credential-asset")
            marker = b"-----BEGIN " + b"PRIVATE KEY-----"
            repository.write(relative, b"\x89PNG\r\n\x1a\n" + marker)
            repository.commit("hide credential in approved asset path")
            self.assert_rule(
                "content.credential", lambda: verify_public_tree(repository.path)
            )

    def test_enforces_total_blob_bound(self):
        self.repository.write("docs/a.md", b"a" * 80)
        self.repository.write("docs/b.md", b"b" * 80)
        self.repository.commit("add bounded files")

        with mock.patch.object(public_release, "MAX_TOTAL_BYTES", 100):
            self.assert_rule(
                "content.total-size", lambda: verify_public_tree(self.repository.path)
            )

    def test_scans_committed_blob_bytes_not_dirty_worktree_content(self):
        private_value = "committed-" + "private-marker"
        self.repository.write("docs/scanned-content.md", private_value)
        self.repository.commit("commit private marker")
        self.repository.write("docs/scanned-content.md", "clean working copy\n")
        policy = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": [private_value],
            }
        )

        error = self.assert_rule(
            "content.private-policy",
            lambda: verify_public_tree(
                self.repository.path, private_policy_path=policy
            ),
        )
        self.assertNotIn(private_value, str(error))
        self.assertEqual(error.relative_path, "docs/scanned-content.md")

        self.repository.write("docs/scanned-content.md", "clean committed copy\n")
        self.repository.commit("commit clean content")
        self.repository.write("docs/scanned-content.md", private_value)
        verify_public_tree(self.repository.path, private_policy_path=policy)

    def test_private_policy_match_in_a_path_is_redacted(self):
        private_value = "private-" + "customer-name"
        relative = f"docs/{private_value}.md"
        self.repository.write(relative, "generic text\n")
        self.repository.commit("add path containing denied value")
        policy = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": [private_value],
            }
        )

        error = self.assert_rule(
            "content.private-policy",
            lambda: verify_public_tree(
                self.repository.path, private_policy_path=policy
            ),
        )
        self.assertNotIn(private_value, str(error))
        self.assertEqual(error.relative_path, "<redacted-path>")


class PublicHistoryTests(PublicReleaseTestCase):
    def denied_file(self, *commit_ids: str) -> Path:
        path = self.root / "pre-public-commit-ids.txt"
        path.write_text("\n".join(commit_ids) + "\n", encoding="ascii")
        path.chmod(0o600)
        return path

    def test_one_root_linear_history_with_only_expected_branch_passes(self):
        denied = self.denied_file("0" * 40)

        verify_public_history(
            self.repository.path,
            expected_branch="main",
            denied_commit_ids_path=denied,
        )

    def test_history_check_remains_callable_in_upload_protocol_clone(self):
        bare = self.root / "export.git"
        clone = self.root / "clone"
        _git(self.repository.path, "clone", "--bare", "--single-branch", ".", str(bare))
        subprocess.run(
            ("git", "clone", "--single-branch", str(bare), str(clone)),
            check=True,
            capture_output=True,
            env=_git_environment(),
            text=True,
        )

        verify_public_history(
            clone,
            expected_branch="main",
            denied_commit_ids_path=self.denied_file("f" * 40),
        )

    def test_rejects_shallow_history(self):
        clone = self.root / "shallow"
        subprocess.run(
            (
                "git",
                "clone",
                "--depth=1",
                "--single-branch",
                self.repository.path.as_uri(),
                str(clone),
            ),
            check=True,
            capture_output=True,
            env=_git_environment(),
            text=True,
        )

        self.assert_rule(
            "history.shallow",
            lambda: verify_public_history(
                clone,
                expected_branch="main",
                denied_commit_ids_path=self.denied_file("f" * 40),
            ),
        )

    def test_binds_expected_branch_and_history_to_checked_out_head(self):
        self.repository.write("docs/architecture.md", "# Architecture\n\nSecond version.\n")
        self.repository.commit("second public commit")
        _git(self.repository.path, "checkout", "--detach", "HEAD^")

        self.assert_rule(
            "history.commit-binding",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=self.denied_file("0" * 40),
            ),
        )

    def test_rejects_missing_expected_branch_and_extra_heads_or_tags(self):
        denied = self.denied_file("0" * 40)
        self.assert_rule(
            "history.branch",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="public",
                denied_commit_ids_path=denied,
            ),
        )

        _git(self.repository.path, "branch", "extra")
        self.assert_rule(
            "history.refs",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )
        _git(self.repository.path, "branch", "-D", "extra")
        _git(self.repository.path, "tag", "v0-private")
        self.assert_rule(
            "history.refs",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

    def test_rejects_refs_in_every_namespace_and_mismatched_transport_aliases(self):
        denied = self.denied_file("0" * 40)
        _git(self.repository.path, "update-ref", "refs/notes/review", self.repository.head())
        self.assert_rule(
            "history.refs",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "direct-remote-head")
            head = repository.head()
            _git(repository.path, "update-ref", "refs/remotes/origin/main", head)
            _git(repository.path, "update-ref", "refs/remotes/origin/HEAD", head)
            self.assert_rule(
                "history.refs",
                lambda: verify_public_history(
                    repository.path,
                    expected_branch="main",
                    denied_commit_ids_path=denied,
                ),
            )
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            repository = DisposableRepository(Path(temporary), "symbolic-remote-branch")
            _git(
                repository.path,
                "symbolic-ref",
                "refs/remotes/origin/main",
                "refs/heads/main",
            )
            _git(
                repository.path,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            )
            self.assert_rule(
                "history.refs",
                lambda: verify_public_history(
                    repository.path,
                    expected_branch="main",
                    denied_commit_ids_path=denied,
                ),
            )
        _git(self.repository.path, "update-ref", "-d", "refs/notes/review")

        first = self.repository.head()
        self.repository.write("docs/architecture.md", "# Architecture\n\nSecond version.\n")
        second = self.repository.commit("second public commit")
        _git(self.repository.path, "update-ref", "refs/remotes/origin/main", first)
        _git(self.repository.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.assertNotEqual(first, second)
        self.assert_rule(
            "history.refs",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

    def test_missing_promisor_blob_cannot_invoke_external_transport_helper(self):
        blob = _git(self.repository.path, "rev-parse", "HEAD:README.md").stdout.strip()
        object_path = self.repository.path / ".git" / "objects" / blob[:2] / blob[2:]
        self.assertTrue(object_path.is_file())
        object_path.unlink()

        helper = self.root / "transport-helper"
        marker = Path(str(helper) + ".ran")
        helper.write_text('#!/bin/sh\ntouch "$0.ran"\nexit 1\n', encoding="utf-8")
        helper.chmod(0o755)
        _git(self.repository.path, "config", "core.repositoryFormatVersion", "1")
        _git(self.repository.path, "config", "extensions.partialClone", "origin")
        _git(self.repository.path, "config", "remote.origin.promisor", "true")
        _git(self.repository.path, "config", "remote.origin.partialclonefilter", "blob:none")
        _git(self.repository.path, "config", "remote.origin.url", f"ext::{helper}")
        _git(self.repository.path, "config", "protocol.ext.allow", "always")

        self.assert_rule(
            "repository.git", lambda: verify_public_tree(self.repository.path)
        )
        self.assertFalse(marker.exists())

    def test_rejects_merge_parents(self):
        denied = self.denied_file("0" * 40)
        _git(self.repository.path, "checkout", "-b", "side")
        self.repository.write("docs/side.md", "side\n")
        self.repository.commit("side")
        _git(self.repository.path, "checkout", "main")
        self.repository.write("docs/main.md", "main\n")
        self.repository.commit("main")
        _git(self.repository.path, "merge", "--no-ff", "side", "-m", "merge side")
        _git(self.repository.path, "branch", "-D", "side")

        self.assert_rule(
            "history.merge",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

    def test_rejects_multiple_roots(self):
        denied = self.denied_file("0" * 40)
        tree = _git(self.repository.path, "rev-parse", "HEAD^{tree}").stdout.strip()
        unrelated = _git(
            self.repository.path,
            "commit-tree",
            tree,
            "-m",
            "unrelated root",
        ).stdout.strip()
        merged = _git(
            self.repository.path,
            "commit-tree",
            tree,
            "-p",
            self.repository.head(),
            "-p",
            unrelated,
            "-m",
            "join roots",
        ).stdout.strip()
        _git(self.repository.path, "update-ref", "refs/heads/main", merged)

        self.assert_rule(
            "history.root-count",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

    def test_rejects_reachable_pre_public_commit_without_printing_id(self):
        private_commit = self.repository.head()
        denied = self.denied_file(private_commit)

        error = self.assert_rule(
            "history.pre-public",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )
        self.assertNotIn(private_commit, str(error))

    def test_rejects_invalid_duplicate_empty_permissive_or_tracked_deny_files(self):
        valid = "0" * 40
        cases = (
            ("", 0o600, "history.deny-file"),
            ("short\n", 0o600, "history.deny-file"),
            (f"{valid}\n{valid}\n", 0o600, "history.deny-file"),
            (f"{valid}\n", 0o644, "history.deny-file"),
        )
        for index, (content, mode, rule) in enumerate(cases):
            with self.subTest(index=index):
                path = self.root / f"deny-{index}.txt"
                path.write_text(content, encoding="ascii")
                path.chmod(mode)
                self.assert_rule(
                    rule,
                    lambda path=path: verify_public_history(
                        self.repository.path,
                        expected_branch="main",
                        denied_commit_ids_path=path,
                    ),
                )

        tracked = self.repository.write("docs/commit-ids.txt", f"{valid}\n")
        tracked.chmod(0o600)
        self.repository.commit("track commit deny list")
        self.assert_rule(
            "history.deny-file",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=tracked,
            ),
        )

        missing = self.root / "missing-parent" / "deny.txt"
        self.assert_rule(
            "history.deny-file",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=missing,
            ),
        )

    def test_requires_git_fsck_success(self):
        denied = self.denied_file("0" * 40)
        object_id = _git(
            self.repository.path, "rev-parse", "HEAD:README.md"
        ).stdout.strip()
        object_path = self.repository.path / ".git/objects" / object_id[:2] / object_id[2:]
        object_path.unlink()

        self.assert_rule(
            "history.fsck",
            lambda: verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            ),
        )

    def test_scrubs_ambient_git_selectors_and_config_injection(self):
        denied = self.denied_file("0" * 40)
        other = DisposableRepository(self.root, "other")
        marker = self.root / "fsmonitor-ran"
        hook = self.root / "fsmonitor-hook"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        poisoned = {
            "GIT_DIR": str(other.path / ".git"),
            "GIT_WORK_TREE": str(other.path),
            "GIT_COMMON_DIR": str(other.path / ".git"),
            "GIT_INDEX_FILE": str(other.path / ".git/index"),
            "GIT_OBJECT_DIRECTORY": str(other.path / ".git/objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other.path / ".git/objects"),
            "GIT_CONFIG": str(self.root / "private-config"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": str(hook),
            "GIT_NAMESPACE": "private",
            "GIT_SHALLOW_FILE": str(other.path / ".git/shallow"),
            "GIT_REPLACE_REF_BASE": "refs/private/replace/",
            "GIT_EXEC_PATH": str(self.root / "private-git-core"),
            "GIT_TEMPLATE_DIR": str(self.root / "private-template"),
            "GIT_NO_LAZY_FETCH": "0",
            "GIT_PROTOCOL_FROM_USER": "1",
            "GIT_ALLOW_PROTOCOL": "ext:file:ssh:https:http",
            "GIT_ASKPASS": str(hook),
            "SSH_ASKPASS": str(hook),
        }
        with mock.patch.dict(os.environ, poisoned, clear=False):
            safe_environment = public_release._safe_git_environment()
            self.assertEqual(safe_environment["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(safe_environment["GIT_PROTOCOL_FROM_USER"], "0")
            self.assertEqual(safe_environment["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(safe_environment["GIT_ASKPASS"], "/usr/bin/false")
            self.assertEqual(safe_environment["SSH_ASKPASS"], "/usr/bin/false")
            verify_public_tree(self.repository.path)
            verify_public_history(
                self.repository.path,
                expected_branch="main",
                denied_commit_ids_path=denied,
            )

        self.assertFalse(marker.exists())


class PublicCiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(CI_WORKFLOW.is_file(), "public CI workflow is missing")
        self.workflow = _load_ci_workflow()
        jobs = self.workflow.get("jobs")
        self.assertIsInstance(jobs, dict)
        self.jobs = jobs

    def test_events_permissions_and_actions_are_read_only_and_secret_free(self):
        self.assertEqual(
            set(self.workflow),
            {"name", "on", "permissions", "jobs"},
        )
        self.assertEqual(
            self.workflow["on"],
            {"pull_request": {}, "workflow_dispatch": {}},
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.jobs), {"python", "node"})

        safe_git_environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
        }

        uses: list[str] = []
        expected_timeouts = {"python": 60, "node": 30}
        for job_name, job in self.jobs.items():
            self.assertIsInstance(job, dict, job_name)
            self.assertNotIn("permissions", job, job_name)
            self.assertEqual(job.get("env"), safe_git_environment, job_name)
            timeout = job.get("timeout-minutes")
            self.assertIsInstance(timeout, int, job_name)
            self.assertEqual(timeout, expected_timeouts[job_name], job_name)
            steps = job.get("steps")
            self.assertIsInstance(steps, list, job_name)
            for step in steps:
                self.assertIsInstance(step, dict, job_name)
                if "uses" in step:
                    self.assertIsInstance(step["uses"], str, job_name)
                    uses.append(step["uses"])

        self.assertEqual(
            uses,
            [
                "actions/checkout@v7",
                "actions/setup-python@v7",
                "actions/setup-node@v7",
                "actions/checkout@v7",
                "actions/setup-node@v7",
            ],
        )
        serialized = json.dumps(self.workflow, sort_keys=True).casefold()
        for forbidden in (
            "secrets.",
            "pull_request_target",
            '"push"',
            '"deployment"',
            '"deployments"',
            '"id-token"',
            '"packages"',
            '"statuses"',
            '"write"',
        ):
            self.assertNotIn(forbidden, serialized)

        for job in self.jobs.values():
            checkout = next(
                step
                for step in job["steps"]
                if step.get("uses") == "actions/checkout@v7"
            )
            self.assertEqual(
                checkout.get("with"),
                {"persist-credentials": False, "set-safe-directory": False},
            )

    def test_supported_runtimes_and_npm_cache_are_explicit(self):
        python_job = self.jobs["python"]
        self.assertEqual(python_job["runs-on"], "macos-15")
        python_setup = next(
            step
            for step in python_job["steps"]
            if step.get("uses") == "actions/setup-python@v7"
        )
        self.assertEqual(python_setup.get("with"), {"python-version": "3.14"})
        python_node_setup = next(
            step
            for step in python_job["steps"]
            if step.get("uses") == "actions/setup-node@v7"
        )
        self.assertEqual(
            python_node_setup.get("with"),
            {
                "node-version": "24",
                "cache": "npm",
                "cache-dependency-path": "package-lock.json",
            },
        )

        node_job = self.jobs["node"]
        self.assertEqual(node_job["runs-on"], "ubuntu-24.04")
        self.assertEqual(
            node_job.get("strategy"),
            {"fail-fast": False, "matrix": {"node": [22, 24, 26]}},
        )
        node_setup = next(
            step
            for step in node_job["steps"]
            if step.get("uses") == "actions/setup-node@v7"
        )
        self.assertEqual(
            node_setup.get("with"),
            {
                "node-version": "${{ matrix.node }}",
                "cache": "npm",
                "cache-dependency-path": "package-lock.json",
            },
        )
    def test_jobs_run_complete_supported_platform_verification(self):
        self.assertEqual(
            _workflow_runs(self.jobs["python"]),
            (
                "npm ci --ignore-scripts",
                "npx playwright install chromium",
                "brew install caddy",
                "caddy version | grep -E '^v2[.]'",
                "python3 -m compileall -q local_web_server scripts tests",
                (
                    "LOCAL_WEB_REQUIRE_CADDY_INTEGRATION=1 "
                    "PYTHONWARNINGS=error::ResourceWarning "
                    "npm run test:python:all"
                ),
                "python3 scripts/verify_host_profile_workflow.py",
            ),
        )
        self.assertEqual(
            _workflow_runs(self.jobs["node"]),
            (
                "npm ci --ignore-scripts",
                "npm run check:frontend",
                "npm run verify:public-release",
                "npm audit",
                "npm audit --omit=dev",
            ),
        )

    def test_jobs_are_bounded_to_disposable_platform_checks(self):
        all_commands = "\n".join(
            command
            for job in self.jobs.values()
            for command in _workflow_runs(job)
        ).casefold()
        for forbidden in (
            "scripts/install_local_web.py",
            "launchctl",
            "tailscale",
            "app activate",
            "apps update",
            "git push",
            "gh repo",
            "config/local/apps.json",
            "host init",
            "host migrate-registry",
            "sudo",
        ):
            self.assertNotIn(forbidden, all_commands)


class RepositoryPublicSurfaceTests(unittest.TestCase):
    def test_index_fixture_and_example_registry_are_generic_and_parseable(self):
        registry_path = ROOT / "config/apps.example.json"
        registry = load_registry(registry_path)
        payload = json.loads(registry_path.read_text(encoding="utf-8"))

        self.assertEqual(registry.schema_version, 2)
        self.assertEqual(registry.public_origin, "https://local.example.ts.net")
        self.assertEqual(payload["runtimeRoot"], "/Users/example/Coding/runtime")
        self.assertGreaterEqual(len(registry.apps), 2)
        self.assertTrue(all(app.id.startswith("example-") for app in registry.apps))
        self.assertTrue(
            all(str(app.repository).startswith("/Users/example/Coding/") for app in registry.apps)
        )

        index = json.loads(
            ROOT.joinpath("apps/system-index/fixtures/registry-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertGreaterEqual(len(index["apps"]), 3)
        self.assertTrue(all(app["id"].startswith("example-") for app in index["apps"]))
        self.assertTrue(all(app["title"].startswith("Example ") for app in index["apps"]))
        self.assertTrue(public_release._index_fixture_is_generic(index))

    def test_public_docs_cover_onboarding_security_operations_and_exports(self):
        documents = {
            "README.md": (
                "## Architecture",
                "## Screenshots",
                "## Prerequisites",
                "## Bootstrap a host",
                "## Create an application",
                "## Security model",
                "## Interactive export",
                "snapshotData",
                "viewState",
                "apps/system-index/tests/snapshots/index-light-desktop.png",
                "## Verification",
                "## Case studies",
                "## Limitations",
                "python3 scripts/install_local_web.py --dry-run",
                "\npython3 scripts/install_local_web.py\n```",
            ),
            "SECURITY.md": (
                "GitHub private vulnerability reporting",
                "Do not post secrets",
            ),
            "CONTRIBUTING.md": (
                "python3 -m unittest discover -s tests -v",
                "npm run verify:host-profile",
                "npm run verify:public-release",
                "npm audit --omit=dev",
            ),
            "docs/architecture.md": (
                "immutable releases",
                "HostProfileStore",
                "trusted-lan",
                "tailscale-serve",
                "127.0.0.1:8080",
            ),
            "docs/operations/host-profile.md": (
                "local-web host init",
                "local-web host migrate-registry",
                "local-web host status",
                "local-web host backup",
                "local-web host restore",
                "local-web host recover",
                ".host-profile-recovery",
                "128 batches",
                "256 MiB",
                "no automatic pruning",
                "excluded from revisions and backups",
                "fails closed",
            ),
            "docs/operations/recovery.md": (
                "local-web host status",
                "local-web host recover",
                "local-web host restore",
                "local-web rollback",
                "scripts/install_local_web.py --dry-run",
                "\npython3 scripts/install_local_web.py\n```",
            ),
        }
        for relative, snippets in documents.items():
            with self.subTest(relative=relative):
                text = ROOT.joinpath(relative).read_text(encoding="utf-8")
                for snippet in snippets:
                    self.assertIn(snippet, text)

    def test_installer_guidance_pairs_exact_preview_and_apply_commands(self):
        preview = "```sh\npython3 scripts/install_local_web.py --dry-run\n```"
        apply = "```sh\npython3 scripts/install_local_web.py\n```"
        for relative in ("README.md", "docs/operations/recovery.md"):
            with self.subTest(relative=relative):
                text = ROOT.joinpath(relative).read_text(encoding="utf-8")
                self.assertIn(preview, text)
                self.assertIn(apply, text)
                self.assertLess(text.index(preview), text.index(apply))

    def test_activation_guidance_uses_private_profile_revision_checkpoint(self):
        stale_phrases = (
            "registry commit",
            "platform-registry commit",
            "committed registry",
            "commit the registry",
        )
        guidance = [ROOT / "README.md"]
        for directory in (ROOT / "docs", ROOT / "skills", ROOT / "templates"):
            guidance.extend(path for path in directory.rglob("*") if path.is_file())
        for path in guidance:
            text = path.read_text(encoding="utf-8").casefold()
            for phrase in stale_phrases:
                self.assertNotIn(phrase, text, str(path.relative_to(ROOT)))

        for relative in (
            "skills/local-web-app-development/references/service-apps.md",
            "skills/local-web-app-development/references/verification.md",
        ):
            with self.subTest(relative=relative):
                text = ROOT.joinpath(relative).read_text(encoding="utf-8")
                self.assertIn("private host-profile registration revision", text)
                self.assertIn("registryRevision", text)
                self.assertIn("retry checkpoint", text)

    def test_private_directory_contract_and_public_tracked_surface_are_exact(self):
        self.assertEqual(
            ROOT.joinpath("config/local/.gitignore").read_text(encoding="utf-8"),
            "*\n!.gitignore\n!README.md\n",
        )
        local_readme = ROOT.joinpath("config/local/README.md").read_text(encoding="utf-8")
        for snippet in (
            "apps.json",
            "history/",
            ".host-profile-transaction.json",
            "backups/",
            "publication-policy.json",
            ".bundle",
            ".sha256",
            "machine backups",
        ):
            self.assertIn(snippet, local_readme)

        tracked = set(_git(ROOT, "ls-files").stdout.splitlines())
        self.assertNotIn("config/apps.json", tracked)
        self.assertFalse(any(path.startswith(".superpowers/") for path in tracked))
        self.assertFalse(any(path.startswith("docs/superpowers/") for path in tracked))
        self.assertEqual(
            {path for path in tracked if path.startswith("config/local/")},
            {"config/local/.gitignore", "config/local/README.md"},
        )

    def test_tracked_public_text_has_no_private_deployment_markers(self):
        forbidden = (
            "/Users/" + "sean",
            "Seans-" + "Mac-mini",
            "tail" + "3c05c0",
            "Financial " + "Planning",
            "Job " + "Hunt",
        )
        tracked = _git(ROOT, "ls-files").stdout.splitlines()
        for relative in tracked:
            if relative.startswith("config/local/"):
                continue
            content = ROOT.joinpath(relative).read_bytes()
            for marker in forbidden:
                self.assertNotIn(marker.encode(), content, relative)


class PublicReleaseScriptTests(PublicReleaseTestCase):
    def run_script(self, *arguments: str, environment: dict[str, str] | None = None):
        return subprocess.run(
            (sys.executable, str(SCRIPT), "--repository", str(self.repository.path), *arguments),
            cwd=ROOT,
            capture_output=True,
            env=_git_environment(**(environment or {})),
            text=True,
            timeout=20,
        )

    def test_wrapper_passes_generic_tree_and_optional_history(self):
        denied = self.root / "deny.txt"
        denied.write_text("0" * 40 + "\n", encoding="ascii")
        denied.chmod(0o600)

        result = self.run_script(
            "--history-branch",
            "main",
            "--deny-commit-ids",
            str(denied),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "public-release verification passed\n")
        self.assertEqual(result.stderr, "")

    def test_wrapper_failure_is_bounded_and_never_prints_private_value(self):
        private_value = "never-" + "print-publication-value"
        self.repository.write("docs/architecture.md", private_value)
        self.repository.commit("commit private marker")
        policy = self.write_private_policy(
            {
                "schema": "local-web-publication-policy/v1",
                "denyStrings": [private_value],
            }
        )

        result = self.run_script("--private-policy", str(policy))

        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("content.private-policy", result.stderr)
        self.assertIn("docs/architecture.md", result.stderr)
        self.assertNotIn(private_value, combined)
        self.assertNotIn(str(self.root), combined)
        self.assertLessEqual(len(combined), 320)

    def test_wrapper_rejects_unpaired_or_unknown_arguments_without_echoing_them(self):
        private_argument = "--unknown-" + "private-value"
        for arguments in (("--history-branch", "main"), (private_argument,)):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                combined = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "public-release: invalid arguments\n")
                self.assertNotIn(private_argument, combined)

    def test_package_exposes_public_release_launcher(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["scripts"].get("verify:public-release"),
            "python3 scripts/verify_public_release.py",
        )


if __name__ == "__main__":
    unittest.main()
