import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.host_profile import (
    HostProfileError,
    HostProfilePaths,
    canonical_json_bytes,
    ensure_secure_directory,
    profile_digest,
    resolve_host_profile,
    validate_secure_directory,
    validate_secure_file,
)


class HostProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def create_repository(self, name="repository"):
        repository = self.root / name
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
            text=True,
        )
        (repository / "config").mkdir()
        return repository

    def create_complete_profile(self, repository):
        paths = HostProfilePaths.for_repository(repository)
        ensure_secure_directory(paths.local, root=paths.repository)
        ensure_secure_directory(paths.history, root=paths.repository)
        ensure_secure_directory(paths.backups, root=paths.repository)
        paths.profile.write_bytes(b'{"apps":[],"schemaVersion":2}\n')
        paths.profile.chmod(0o600)
        return paths

    def assert_unsafe(self, action, repository):
        with self.assertRaises(HostProfileError) as raised:
            action()
        message = str(raised.exception)
        self.assertLessEqual(len(message), 160)
        self.assertNotIn(str(repository), message)

    def test_paths_use_only_the_canonical_private_layout(self):
        repository = self.create_repository()

        paths = HostProfilePaths.for_repository(repository)

        canonical_repository = repository.resolve()
        self.assertEqual(paths.repository, canonical_repository)
        self.assertEqual(paths.config, canonical_repository / "config")
        self.assertEqual(paths.local, canonical_repository / "config/local")
        self.assertEqual(
            paths.profile, canonical_repository / "config/local/apps.json"
        )
        self.assertEqual(paths.history, canonical_repository / "config/local/history")
        self.assertEqual(paths.backups, canonical_repository / "config/local/backups")
        self.assertEqual(
            paths.transaction,
            canonical_repository / "config/local/.host-profile-transaction.json",
        )
        self.assertEqual(
            paths.profile_temporary,
            canonical_repository / "config/local/.apps.json.tmp",
        )
        self.assertEqual(
            paths.revision_temporary,
            canonical_repository / "config/local/history/.revision.json.tmp",
        )
        self.assertEqual(
            paths.transaction_temporary,
            canonical_repository / "config/local/.host-profile-transaction.json.tmp",
        )
        self.assertEqual(
            paths.backup_temporary,
            canonical_repository / "config/local/backups/.host-profile-backup.json.tmp",
        )

    def test_repository_input_is_canonicalised_before_paths_are_derived(self):
        repository = self.create_repository()
        alias = self.root / "repository-alias"
        alias.symlink_to(repository, target_is_directory=True)

        paths = HostProfilePaths.for_repository(alias)

        canonical_repository = repository.resolve()
        self.assertEqual(paths.repository, canonical_repository)
        self.assertEqual(
            paths.profile, canonical_repository / "config/local/apps.json"
        )

    def test_paths_preserve_the_selected_repository_inode_identity(self):
        repository = self.create_repository()
        paths = HostProfilePaths.for_repository(repository)
        metadata = repository.stat()

        self.assertEqual(paths.repository_device, metadata.st_dev)
        self.assertEqual(paths.repository_inode, metadata.st_ino)

        displaced = repository.with_name("displaced-repository")
        repository.rename(displaced)
        self.create_repository()
        replacement = HostProfilePaths.for_repository(repository)
        self.assertNotEqual(paths, replacement)

    def test_non_repository_and_nested_repository_paths_are_rejected(self):
        not_repository = self.root / "not-a-repository"
        not_repository.mkdir()
        (not_repository / "config").mkdir()
        repository = self.create_repository()
        nested = repository / "nested"
        nested.mkdir()

        for candidate in (not_repository, nested, self.root / "missing"):
            with self.subTest(candidate=candidate.name):
                self.assert_unsafe(
                    lambda candidate=candidate: HostProfilePaths.for_repository(candidate),
                    candidate,
                )

    def test_ambient_git_selection_cannot_make_a_non_repository_valid(self):
        genuine_repository = self.create_repository("genuine-repository")
        not_repository = self.root / "not-a-repository"
        not_repository.mkdir()
        (not_repository / "config").mkdir()
        environment = {
            "GIT_DIR": str(genuine_repository / ".git"),
            "GIT_WORK_TREE": str(not_repository),
        }

        with patch.dict(os.environ, environment):
            self.assert_unsafe(
                lambda: HostProfilePaths.for_repository(not_repository),
                not_repository,
            )

    def test_conflicting_ambient_git_selection_does_not_hide_a_genuine_root(self):
        repository = self.create_repository("repository")
        conflicting_repository = self.create_repository("conflicting-repository")
        environment = {
            "GIT_DIR": str(conflicting_repository / ".git"),
            "GIT_WORK_TREE": str(conflicting_repository),
        }

        with patch.dict(os.environ, environment):
            paths = HostProfilePaths.for_repository(repository)

        self.assertEqual(paths.repository, repository.resolve())
        self.assertEqual(paths.profile, repository.resolve() / "config/local/apps.json")

    def test_repository_resolution_never_executes_git_from_ambient_path(self):
        repository = self.create_repository("repository")
        malicious = self.root / "ambient-bin"
        malicious.mkdir()
        marker = self.root / "ambient-git-ran"
        executable = malicious / "git"
        executable.write_text(
            f"#!/bin/sh\ntouch {marker!s}\nexit 91\n", encoding="utf-8"
        )
        executable.chmod(0o700)

        with patch.dict(os.environ, {"PATH": os.fspath(malicious)}):
            paths = HostProfilePaths.for_repository(repository)

        self.assertEqual(paths.repository, repository.resolve())
        self.assertFalse(marker.exists())

    def test_missing_profile_directs_to_init_or_restore_without_a_path(self):
        repository = self.create_repository()

        with self.assertRaisesRegex(
            HostProfileError,
            r"local-web host init.*local-web host restore",
        ) as raised:
            resolve_host_profile(repository)

        self.assertNotIn(str(repository), str(raised.exception))

    def test_public_example_and_legacy_registry_are_never_fallbacks(self):
        repository = self.create_repository()
        (repository / "config/apps.json").write_text("legacy private bytes")
        (repository / "config/apps.example.json").write_text("public example bytes")

        with self.assertRaises(HostProfileError):
            resolve_host_profile(repository)

    def test_resolver_returns_the_canonical_secure_profile(self):
        repository = self.create_repository()
        paths = self.create_complete_profile(repository)

        resolved = resolve_host_profile(repository)

        self.assertEqual(resolved, paths.profile)

    def test_symlinked_config_and_local_directories_are_rejected(self):
        for component in ("config", "local"):
            with self.subTest(component=component):
                repository = self.create_repository(f"repository-{component}")
                external = self.root / f"external-{component}"
                external.mkdir()
                if component == "config":
                    (repository / "config").rmdir()
                    (repository / "config").symlink_to(
                        external, target_is_directory=True
                    )
                else:
                    (repository / "config/local").symlink_to(
                        external, target_is_directory=True
                    )

                self.assert_unsafe(
                    lambda repository=repository: HostProfilePaths.for_repository(
                        repository
                    ),
                    repository,
                )

    def test_symlinked_profile_directories_marker_and_temporaries_are_rejected(self):
        component_names = (
            "profile",
            "history",
            "backups",
            "transaction",
            "profile_temporary",
            "revision_temporary",
            "transaction_temporary",
            "backup_temporary",
        )
        for component_name in component_names:
            with self.subTest(component=component_name):
                repository = self.create_repository(f"repository-{component_name}")
                paths = HostProfilePaths.for_repository(repository)
                ensure_secure_directory(paths.local, root=paths.repository)
                ensure_secure_directory(paths.history, root=paths.repository)
                ensure_secure_directory(paths.backups, root=paths.repository)
                paths.profile.write_bytes(b"{}\n")
                paths.profile.chmod(0o600)
                candidate = getattr(paths, component_name)
                if candidate.exists():
                    if candidate.is_dir():
                        candidate.rmdir()
                    else:
                        candidate.unlink()
                external = self.root / f"external-{component_name}"
                external.write_bytes(b"private bytes")
                candidate.symlink_to(external)

                self.assert_unsafe(
                    lambda repository=repository: resolve_host_profile(repository),
                    repository,
                )

    def test_private_directories_are_created_with_exact_mode(self):
        repository = self.create_repository()
        paths = HostProfilePaths.for_repository(repository)

        for directory in (paths.local, paths.history, paths.backups):
            ensure_secure_directory(directory, root=paths.repository)

        for directory in (paths.local, paths.history, paths.backups):
            with self.subTest(directory=directory.name):
                self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), 0o700)

    def test_new_private_directory_mode_is_exact_under_a_restrictive_umask(self):
        repository = self.create_repository()
        paths = HostProfilePaths.for_repository(repository)
        previous_umask = os.umask(0o777)
        try:
            ensure_secure_directory(paths.local, root=paths.repository)
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(paths.local.lstat().st_mode), 0o700)

    def test_directory_creation_cannot_escape_after_an_ancestor_swap(self):
        repository = self.create_repository()
        paths = HostProfilePaths.for_repository(repository)
        displaced_config = repository / "displaced-config"
        external = self.root / "external"
        external.mkdir()
        original_mkdir = os.mkdir
        swapped = False

        def swap_ancestor_then_mkdir(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                (repository / "config").rename(displaced_config)
                (repository / "config").symlink_to(
                    external, target_is_directory=True
                )
                swapped = True
            return original_mkdir(*args, **kwargs)

        with (
            patch(
                "local_web_server.host_profile.os.mkdir",
                side_effect=swap_ancestor_then_mkdir,
            ),
            self.assertRaises(HostProfileError),
        ):
            ensure_secure_directory(paths.local, root=paths.repository)

        self.assertTrue(swapped)
        self.assertFalse((external / "local").exists())

    def test_secure_directory_and_file_validators_accept_exact_modes(self):
        repository = self.create_repository()
        paths = self.create_complete_profile(repository)

        validate_secure_directory(paths.local, root=paths.repository)
        validate_secure_directory(paths.history, root=paths.repository)
        validate_secure_directory(paths.backups, root=paths.repository)
        validate_secure_file(paths.profile, root=paths.repository)

    def test_existing_permissive_private_directories_are_rejected_not_repaired(self):
        repository = self.create_repository()
        paths = HostProfilePaths.for_repository(repository)
        paths.local.mkdir(mode=0o700)
        paths.local.chmod(0o750)

        self.assert_unsafe(
            lambda: ensure_secure_directory(paths.local, root=paths.repository),
            repository,
        )
        self.assertEqual(stat.S_IMODE(paths.local.lstat().st_mode), 0o750)

    def test_public_scaffold_requires_explicit_selection_and_exact_public_entries(self):
        empty_repository = self.create_repository("empty-public-scaffold")
        empty_local = empty_repository / "config/local"
        empty_local.mkdir(mode=0o755)
        empty_local.chmod(0o755)
        self.assertEqual(
            HostProfilePaths.for_repository(
                empty_repository, allow_public_scaffold=True
            ).local,
            empty_local.resolve(),
        )

        repository = self.create_repository()
        local = repository / "config/local"
        local.mkdir(mode=0o755)
        (local / ".gitignore").write_text("public ignore rules\n")
        (local / "README.md").write_text("public instructions\n")

        self.assert_unsafe(
            lambda: HostProfilePaths.for_repository(repository),
            repository,
        )
        paths = HostProfilePaths.for_repository(
            repository, allow_public_scaffold=True
        )
        self.assertEqual(paths.local, local.resolve())
        self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o755)

        (local / "unexpected.json").write_text("private state\n")
        self.assert_unsafe(
            lambda: HostProfilePaths.for_repository(
                repository, allow_public_scaffold=True
            ),
            repository,
        )
        self.assert_unsafe(
            lambda: HostProfilePaths.for_repository(
                repository, allow_public_scaffold="yes"
            ),
            repository,
        )

    def test_existing_permissive_private_files_are_rejected(self):
        repository = self.create_repository()
        paths = self.create_complete_profile(repository)
        paths.profile.chmod(0o640)

        self.assert_unsafe(
            lambda: resolve_host_profile(repository),
            repository,
        )
        self.assertEqual(stat.S_IMODE(paths.profile.lstat().st_mode), 0o640)

    def test_secure_helpers_reject_paths_outside_the_repository_root(self):
        repository = self.create_repository()
        outside_directory = self.root / "outside-directory"
        outside_directory.mkdir(mode=0o700)
        outside_directory.chmod(0o700)
        outside_file = self.root / "outside-file"
        outside_file.write_bytes(b"secret")
        outside_file.chmod(0o600)

        self.assert_unsafe(
            lambda: validate_secure_directory(
                outside_directory, root=repository
            ),
            repository,
        )
        self.assert_unsafe(
            lambda: validate_secure_file(outside_file, root=repository),
            repository,
        )

    def test_profile_digest_is_lowercase_sha256_of_exact_bytes(self):
        first = b'{"apps":[]}\n'
        second = b'{"apps":[]} '

        self.assertEqual(
            profile_digest(first),
            "209eaa64f935ca769047e32e9581ed43bcc9dfc437a2e783b12a325be94d6553",
        )
        self.assertEqual(
            profile_digest(second),
            "a2a12e7ebd244b3a4c6d114998730e3bbebf1fa0ee8a384df004065953f7f7c3",
        )

    def test_canonical_json_is_sorted_compact_utf8_with_terminal_newline(self):
        value = {"z": [3, 2, 1], "message": "café", "a": {"b": True}}

        encoded = canonical_json_bytes(value)

        self.assertEqual(
            encoded,
            b'{"a":{"b":true},"message":"caf\xc3\xa9","z":[3,2,1]}\n',
        )
        self.assertEqual(encoded.count(b"\n"), 1)


if __name__ == "__main__":
    unittest.main()
