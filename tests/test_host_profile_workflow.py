import importlib
import os
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

    def test_recovery_backup_requires_exact_final_snapshot_bytes(self):
        profile = b'{"host":"profile.invalid"}\n'
        revision_bytes = (b"first revision\n", b"second revision\n")
        snapshot = SimpleNamespace(
            profile_bytes=profile,
            revisions=tuple(
                SimpleNamespace(envelope_bytes=content) for content in revision_bytes
            ),
        )

        def document(profile_bytes=profile, revisions=revision_bytes):
            return SimpleNamespace(
                profile_bytes=profile_bytes,
                revisions=tuple(
                    SimpleNamespace(content=content) for content in revisions
                ),
            )

        profile_verifier._verify_backup_matches_snapshot(document(), snapshot)
        for name, corrupted in (
            ("profile", document(profile_bytes=b'{"wrong":"profile"}\n')),
            ("revision-order", document(revisions=tuple(reversed(revision_bytes)))),
            ("revision-bytes", document(revisions=(revision_bytes[0], b"changed\n"))),
        ):
            with self.subTest(corruption=name):
                with self.assertRaises(profile_verifier.HostProfileWorkflowError):
                    profile_verifier._verify_backup_matches_snapshot(corrupted, snapshot)

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

    def test_public_runner_bounds_private_output_and_cleans_root(self):
        for fails in (False, True):
            with self.subTest(workflow_fails=fails):
                roots: list[Path] = []
                lines: list[str] = []
                private_marker = "private-host-profile-sentinel"

                def tiny_workflow(root, *, caddy):
                    roots.append(root)
                    (root / "private-profile").write_text(
                        private_marker, encoding="utf-8"
                    )
                    if fails:
                        raise RuntimeError(f"{root}: {private_marker}")
                    return SimpleNamespace(
                        phases=profile_verifier.PHASES[:-1],
                        directory_modes=(0o700, 0o700, 0o700),
                        file_modes=(0o600, 0o600, 0o600),
                        revision_counts=(1, 1),
                        caddy_validated=True,
                        all_paths_disposable=True,
                        status_redacted=True,
                        backup_round_tripped=True,
                        recovery_residue_retained=True,
                        owned_paths=(root / private_marker,),
                    )

                verifier = profile_verifier.HostProfileWorkflowVerifier(
                    emit=lines.append
                )
                with (
                    mock.patch.object(profile_verifier, "run_workflow", tiny_workflow),
                    mock.patch.object(
                        profile_verifier, "select_caddy", return_value=Path("unused")
                    ),
                ):
                    self.assertEqual(verifier.run(), 1 if fails else 0)
                self.assertEqual(len(roots), 1)
                self.assertFalse(roots[0].exists())
                output = "\n".join(lines)
                self.assertGreater(len(output), 0)
                self.assertLess(len(output), 2048)
                for private_value in (
                    private_marker, str(roots[0]), tempfile.gettempdir(), str(ROOT)
                ):
                    self.assertNotIn(private_value, output)


if __name__ == "__main__":
    unittest.main()
