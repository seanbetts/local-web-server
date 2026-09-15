import importlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scripts.verify_host_profile_workflow as profile_verifier
from tests.helpers import TEST_CADDY, caddy_integration, select_test_caddy


ROOT = Path(__file__).parents[1]


CADDY = TEST_CADDY


class HostProfileWorkflowTests(unittest.TestCase):
    def test_required_caddy_integration_fails_instead_of_skipping(self):
        def unavailable() -> Path:
            raise profile_verifier.HostProfileWorkflowError(
                "Caddy validation is unavailable"
            )

        self.assertIsNone(select_test_caddy(select=unavailable, required=False))
        with self.assertRaisesRegex(RuntimeError, "required Caddy integration"):
            select_test_caddy(select=unavailable, required=True)


    def test_shared_environment_removes_every_ambient_git_control(self):
        module_name = "scripts.disposable_workflow_support"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "the shared disposable workflow support module is missing",
        )
        support = importlib.import_module(module_name)
        poisoned = {
            "GIT_DIR": "/private/repository.git",
            "GIT_WORK_TREE": "/private/worktree",
            "GIT_COMMON_DIR": "/private/common",
            "GIT_INDEX_FILE": "/private/index",
            "GIT_OBJECT_DIRECTORY": "/private/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/private/alternates",
            "GIT_CONFIG": "/private/config",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/private/hooks",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "/private/monitor",
            "GIT_CEILING_DIRECTORIES": "/private",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
            "GIT_NAMESPACE": "private",
            "GIT_SHALLOW_FILE": "/private/shallow",
            "GIT_REPLACE_REF_BASE": "refs/private/replace/",
            "GIT_EXEC_PATH": "/private/git-core",
            "GIT_TEMPLATE_DIR": "/private/templates",
        }
        with mock.patch.dict(
            os.environ,
            {**poisoned, "WORKFLOW_SAFE_SENTINEL": "preserved"},
            clear=False,
        ):
            environment = support.sanitized_subprocess_environment(
                overrides={"PYTHONDONTWRITEBYTECODE": "1"}
            )

        self.assertEqual(environment["WORKFLOW_SAFE_SENTINEL"], "preserved")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertFalse(set(poisoned) & set(environment))
        self.assertFalse(any(name.startswith("GIT_") for name in environment))

    def test_shared_profile_helper_enforces_complete_initial_store(self):
        module_name = "scripts.disposable_workflow_support"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "the shared disposable workflow support module is missing",
        )
        support = importlib.import_module(module_name)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            platform = root / "platform"
            profile_verifier._create_repository(platform)
            profile_verifier._commit_fixture(platform)
            content = profile_verifier._registry_bytes(
                root,
                root / "app",
                host="shared-profile.invalid",
            )

            profile = support.initialise_disposable_host_profile(
                platform, content
            )
            paths = profile_verifier.HostProfilePaths.for_repository(platform)
            store = profile_verifier.HostProfileStore(paths)
            snapshot = store.read_snapshot(
                max_profile_bytes=5 * 1024 * 1024,
                max_revision_bytes=8 * 1024 * 1024,
                max_revisions=10_000,
                max_total_bytes=64 * 1024 * 1024,
            )

            self.assertEqual(profile, platform / "config/local/apps.json")
            self.assertEqual(snapshot.profile_bytes, content)
            self.assertEqual(len(snapshot.revisions), 1)
            self.assertEqual(snapshot.revisions[0].registry_bytes, content)
            self.assertIsNone(snapshot.revisions[0].previous_registry_sha256)
            self.assertEqual(snapshot.revisions[0].operation, "initialisation")
            self.assertIsNone(snapshot.revisions[0].app_id)
            self.assertEqual(
                [path.name for path in paths.history.iterdir()],
                [
                    "00000000000000000001-"
                    f"{snapshot.revisions[0].revision_id}.json"
                ],
            )
            for directory in (paths.local, paths.history, paths.backups):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for private_file in (paths.profile, *paths.history.iterdir()):
                self.assertEqual(private_file.stat().st_mode & 0o777, 0o600)
            self.assertFalse(paths.transaction.exists())
            self.assertFalse(paths.recovery_residue.exists())
            self.assertFalse(any(path.exists() for path in paths.temporary_files))

    def test_workflow_git_runners_cannot_mutate_poisoned_external_repository(self):
        import scripts.verify_fleet_update_workflow as fleet_verifier
        import scripts.verify_foundation_adoption_workflow as foundation_verifier
        import scripts.verify_new_app_workflow as new_app_verifier
        import scripts.verify_public_base_path_migration as public_verifier
        import scripts.verify_repository_service_transition as transition_verifier
        import scripts.verify_service_command_migration as service_verifier

        runners = {
            "host-profile": lambda repository, branch: profile_verifier._git(
                repository, "switch", "-C", branch
            ),
            "new-app": lambda repository, branch: new_app_verifier._run_git(
                repository, "switch", "-C", branch
            ),
            "fleet-update": lambda repository, branch: fleet_verifier._git(
                repository, "switch", "-C", branch
            ),
            "foundation-adoption": lambda repository, branch: foundation_verifier._git(
                repository, "switch", "-C", branch
            ),
            "repository-service-transition": (
                lambda repository, branch: transition_verifier._run_git(
                    repository, "switch", "-C", branch
                )
            ),
            "service-command-migration": (
                lambda repository, branch: service_verifier._run_git(
                    repository, "switch", "-C", branch
                )
            ),
            "public-base-path-migration": (
                lambda repository, branch: public_verifier._run_git(
                    repository, "switch", "-C", branch
                )
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for index, (name, runner) in enumerate(runners.items()):
                with self.subTest(workflow=name):
                    target = root / f"target-{index}"
                    external = root / f"external-{index}"
                    profile_verifier._create_repository(target)
                    profile_verifier._commit_fixture(target)
                    profile_verifier._create_repository(external)
                    profile_verifier._commit_fixture(external)
                    external_sentinel = external / "external-sentinel.txt"
                    external_sentinel.write_text("unchanged\n", encoding="utf-8")
                    profile_verifier._git(external, "add", "external-sentinel.txt")
                    profile_verifier._commit_fixture(external)
                    external_head = profile_verifier._git(
                        external, "rev-parse", "HEAD"
                    )
                    external_refs = profile_verifier._git(
                        external,
                        "for-each-ref",
                        "--format=%(refname):%(objectname)",
                    )
                    hook_marker = root / f"hook-{index}-escaped"
                    hook_directory = root / f"hooks-{index}"
                    hook_directory.mkdir()
                    hook = hook_directory / "post-checkout"
                    hook.write_text(
                        "#!/bin/sh\n"
                        f"/usr/bin/touch {str(hook_marker)!r}\n",
                        encoding="utf-8",
                    )
                    hook.chmod(0o700)
                    config = root / f"git-config-{index}"
                    config.write_text(
                        f"[core]\n\thooksPath = {hook_directory}\n",
                        encoding="utf-8",
                    )
                    branch = f"workflow-target-{index}"
                    poisoned = {
                        "GIT_DIR": str(external / ".git"),
                        "GIT_WORK_TREE": str(external),
                        "GIT_COMMON_DIR": str(external / ".git"),
                        "GIT_INDEX_FILE": str(external / ".git/index"),
                        "GIT_OBJECT_DIRECTORY": str(external / ".git/objects"),
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                            external / ".git/objects"
                        ),
                        "GIT_CONFIG": str(config),
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "core.hooksPath",
                        "GIT_CONFIG_VALUE_0": str(hook_directory),
                    }
                    try:
                        with mock.patch.dict(os.environ, poisoned, clear=False):
                            runner(target, branch)
                    except BaseException as error:
                        self.fail(
                            f"{name} accepted ambient Git redirection: "
                            f"{type(error).__name__}"
                        )

                    self.assertEqual(
                        profile_verifier._git(target, "branch", "--show-current"),
                        branch,
                    )
                    self.assertEqual(
                        profile_verifier._git(external, "rev-parse", "HEAD"),
                        external_head,
                    )
                    self.assertEqual(
                        profile_verifier._git(
                            external,
                            "for-each-ref",
                            "--format=%(refname):%(objectname)",
                        ),
                        external_refs,
                    )
                    self.assertEqual(
                        external_sentinel.read_text(encoding="utf-8"),
                        "unchanged\n",
                    )
                    self.assertEqual(
                        profile_verifier._git(external, "status", "--short"),
                        "",
                    )
                    self.assertFalse(hook_marker.exists())

    @caddy_integration
    def test_recovery_backup_requires_exact_final_snapshot_bytes(self):
        real_parse = profile_verifier.parse_host_backup
        corruptions = {
            "profile": lambda document: SimpleNamespace(
                profile_bytes=b'{"wrong":"profile"}\n',
                revisions=document.revisions,
                document_bytes=document.document_bytes,
            ),
            "revision-order": lambda document: SimpleNamespace(
                profile_bytes=document.profile_bytes,
                revisions=tuple(reversed(document.revisions)),
                document_bytes=document.document_bytes,
            ),
        }
        for name, corrupt in corruptions.items():
            with (
                self.subTest(corruption=name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()

                def corrupt_final_backup(path: Path):
                    document = real_parse(path)
                    if Path(path).is_relative_to(
                        root / "restored/config/local/backups"
                    ):
                        return corrupt(document)
                    return document

                with mock.patch.object(
                    profile_verifier,
                    "parse_host_backup",
                    side_effect=corrupt_final_backup,
                ):
                    with self.assertRaises(
                        profile_verifier.HostProfileWorkflowError
                    ):
                        profile_verifier.run_workflow(root, caddy=CADDY)

    def test_caddy_lookup_prefers_executable_homebrew_then_portable_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            homebrew = root / "homebrew-caddy"
            fallback = root / "portable-caddy"
            homebrew.write_text("#!/bin/sh\n", encoding="utf-8")
            fallback.write_text("#!/bin/sh\n", encoding="utf-8")
            homebrew.chmod(0o700)
            fallback.chmod(0o700)

            self.assertEqual(
                profile_verifier.select_caddy(
                    homebrew=homebrew, which=lambda _name: str(fallback)
                ),
                homebrew,
            )

            homebrew.chmod(0o600)
            self.assertEqual(
                profile_verifier.select_caddy(
                    homebrew=homebrew, which=lambda _name: str(fallback)
                ),
                fallback,
            )

            fallback.chmod(0o600)
            with self.assertRaises(profile_verifier.HostProfileWorkflowError):
                profile_verifier.select_caddy(
                    homebrew=homebrew, which=lambda _name: str(fallback)
                )

    @caddy_integration
    def test_complete_workflow_uses_only_disposable_private_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            evidence = profile_verifier.run_workflow(root, caddy=CADDY)

            self.assertEqual(evidence.phases, profile_verifier.PHASES[:-1])
            self.assertEqual(evidence.profile_relative, Path("platform/config/local/apps.json"))
            self.assertEqual(evidence.restored_profile_relative, Path("restored/config/local/apps.json"))
            self.assertEqual(evidence.directory_modes, (0o700, 0o700, 0o700))
            self.assertEqual(evidence.file_modes, (0o600, 0o600, 0o600))
            self.assertEqual(evidence.revision_counts, (1, 1))
            self.assertEqual(
                evidence.recovery_actions,
                (
                    "remove-untouched-marker",
                    "publish-candidate",
                    "clear-marker",
                ),
            )
            self.assertTrue(evidence.caddy_validated)
            self.assertTrue(evidence.all_paths_disposable)
            self.assertTrue(evidence.status_redacted)
            self.assertTrue(evidence.backup_round_tripped)
            self.assertTrue(evidence.recovery_residue_retained)

            for path in evidence.owned_paths:
                self.assertTrue(path.is_relative_to(root))

    @caddy_integration
    def test_public_runner_emits_only_bounded_phase_labels_and_cleans_root(self):
        roots: list[Path] = []
        lines: list[str] = []

        class RecordingTemporaryDirectory:
            def __init__(inner_self, *args, **kwargs):
                inner_self.temporary = tempfile.TemporaryDirectory(*args, **kwargs)
                inner_self.name = inner_self.temporary.name
                roots.append(Path(inner_self.name))

            def cleanup(inner_self):
                inner_self.temporary.cleanup()

        verifier = profile_verifier.HostProfileWorkflowVerifier(
            temporary_directory_factory=RecordingTemporaryDirectory,
            emit=lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in profile_verifier.PHASES])
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        output = "\n".join(lines)
        self.assertLess(len(output), 2048)
        self.assertNotIn(tempfile.gettempdir(), output)
        self.assertNotIn(str(ROOT), output)


if __name__ == "__main__":
    unittest.main()
