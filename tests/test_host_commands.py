import io
import json
import os
import stat
import sys
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from local_web_server.host_commands import HostCommands, HostCommandError
from local_web_server.host_profile import canonical_json_bytes
from local_web_server.host_profile_backup import parse_host_backup, MAX_PROFILE_BYTES
from local_web_server.host_profile_backup import build_host_backup
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError
from tests.test_host_profile_store import StoreFixture, BEFORE, AFTER, CLOCK, digest, envelope


macos_apply = unittest.skipUnless(sys.platform == "darwin", "requires macOS atomic private publication")


@contextmanager
def forbid_writes():
    """Catch transient mutations too, not just files remaining after preview."""
    real_open = os.open
    def read_only_open(path, flags, *args, **kwargs):
        if (
            os.fspath(path) != os.devnull
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            raise AssertionError("preview attempted a write")
        return real_open(path, flags, *args, **kwargs)
    with patch("os.open", side_effect=read_only_open), patch("os.mkdir", side_effect=AssertionError("preview mkdir")), patch("os.unlink", side_effect=AssertionError("preview unlink")), patch("os.replace", side_effect=AssertionError("preview replace")):
        yield


class HostCommandTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / "prepared.json"
        self.write_private(self.source, BEFORE)
        self.commands = HostCommands(self.root, clock=CLOCK)

    def assert_command_error(self, action):
        with self.assertRaises(HostCommandError) as raised:
            action()
        message = str(raised.exception)
        self.assertLessEqual(len(message), 160)
        for secret in (str(self.root), "localhost", "example.test", "/tmp/runtime"):
            self.assertNotIn(secret, message)

    def tree(self):
        return {str(path.relative_to(self.root)): (stat.S_IMODE(path.lstat().st_mode), path.read_bytes() if path.is_file() else None)
                for path in self.root.rglob("*") if ".git" not in path.parts}

    def create_tracked_public_scaffold(self):
        self.paths.local.mkdir(mode=0o755)
        self.paths.local.chmod(0o755)
        (self.paths.local / ".gitignore").write_text("public ignore rules\n")
        (self.paths.local / "README.md").write_text("public instructions\n")
        self.git("add", "-f", "config/local/.gitignore", "config/local/README.md")
        self.git(
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false",
            "-c", "user.name=Test",
            "-c", "user.email=test@example.test",
            "commit", "-qm", "public scaffold",
        )

    def test_init_preview_is_frozen_exact_and_absolutely_read_only(self):
        before = self.tree()
        with forbid_writes():
            plan = self.commands.preview_init(self.source)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.paths.local.exists())
        self.assertEqual(plan.profile_sha256, digest(BEFORE))
        self.assertEqual(plan.operation, "init")
        self.assertEqual(plan.action, "initialise")
        self.assertEqual(plan.planned_modes, ("directories=0700", "files=0600"))
        with self.assertRaises((AttributeError, TypeError)):
            plan.profile_sha256 = "0" * 64

    def test_init_preview_accepts_public_scaffold_without_mutating_it(self):
        self.create_tracked_public_scaffold()
        commands = HostCommands(self.root, clock=CLOCK)
        before = self.tree()

        with forbid_writes():
            plan = commands.preview_init(self.source)

        self.assertEqual(self.tree(), before)
        self.assertEqual(stat.S_IMODE(self.paths.local.stat().st_mode), 0o755)
        self.assertEqual(plan.action, "initialise")

    def test_init_rejects_permissive_local_directory_with_non_scaffold_state(self):
        self.create_tracked_public_scaffold()
        (self.paths.local / "unexpected.json").write_text("private state\n")

        self.assert_command_error(lambda: HostCommands(self.root, clock=CLOCK))

    @macos_apply
    def test_init_apply_hardens_public_scaffold_before_private_publication(self):
        self.create_tracked_public_scaffold()
        commands = HostCommands(self.root, clock=CLOCK)
        public_files = {
            path.name: path.read_bytes()
            for path in self.paths.local.iterdir()
        }

        result = commands.apply(commands.preview_init(self.source))

        self.assertTrue(result.applied)
        self.assertEqual(stat.S_IMODE(self.paths.local.stat().st_mode), 0o700)
        self.assertEqual(commands.store.read_current(), BEFORE)
        self.assertEqual(len(commands.store.revisions()), 1)
        self.assertEqual(
            {
                name: (self.paths.local / name).read_bytes()
                for name in public_files
            },
            public_files,
        )

    @macos_apply
    def test_init_apply_creates_exact_profile_revision_and_verified_backup_modes(self):
        result = self.commands.apply(self.commands.preview_init(self.source))
        self.assertTrue(result.applied)
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
        self.assertEqual(len(self.store.revisions()), 1)
        self.assertEqual(result.as_dict()["publishedRevisionId"], self.store.revisions()[0].revision_id)
        self.assertIsNone(result.as_dict()["revisionId"], "plan retains the observed pre-apply state")
        backup = parse_host_backup(result.backup_path)
        self.assertEqual(backup.profile_bytes, BEFORE)
        self.assertEqual(backup.revisions[0].content, self.store.revisions()[0].envelope_bytes)
        for directory in (self.paths.local, self.paths.history, self.paths.backups):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in directory.iterdir():
                if path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.source.read_bytes(), BEFORE)

    def test_init_rejects_private_history_backups_and_existing_state(self):
        self.store.initialise(BEFORE)
        for source in (self.paths.profile, next(self.paths.history.iterdir())):
            self.assert_command_error(lambda: self.commands.preview_init(source))
        self.write_private(self.paths.backups / "prepared.json", BEFORE)
        self.assert_command_error(lambda: self.commands.preview_init(self.paths.backups / "prepared.json"))
        self.paths.profile.unlink()
        self.assert_command_error(lambda: self.commands.preview_init(self.source))
        for path in self.paths.history.iterdir():
            path.unlink()
        self.assert_command_error(lambda: self.commands.preview_init(self.source))

    def test_init_rejects_pending_state_without_parsing_or_removing_it(self):
        self.paths.local.mkdir(mode=0o700)
        self.write_private(self.paths.transaction, b"incomplete transaction")
        before = self.tree()
        self.assert_command_error(lambda: self.commands.preview_init(self.source))
        self.assertEqual(self.tree(), before)

    def test_init_rejects_changed_source_or_destination_at_apply(self):
        plan = self.commands.preview_init(self.source)
        self.write_private(self.source, AFTER)
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.local.exists())
        self.write_private(self.source, BEFORE)
        self.paths.local.mkdir(mode=0o700)
        self.paths.history.mkdir(mode=0o700)
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.profile.exists())

    def test_init_bounded_source_file_checks_fail_closed(self):
        for content in (b"invalid secret", b"[" * 3000 + b"0" + b"]" * 3000, b"{}"):
            self.write_private(self.source, content)
            self.assert_command_error(lambda: self.commands.preview_init(self.source))
        self.write_private(self.source, BEFORE)
        self.source.chmod(0o644)
        self.assert_command_error(lambda: self.commands.preview_init(self.source))
        self.source.chmod(0o600)
        linked = self.root / "linked.json"
        linked.symlink_to(self.source)
        self.assert_command_error(lambda: self.commands.preview_init(linked))
        linked.unlink()
        os.link(self.source, linked)
        self.assert_command_error(lambda: self.commands.preview_init(linked))
        linked.unlink()
        with self.source.open("wb") as handle:
            handle.truncate(MAX_PROFILE_BYTES + 1)
        self.assert_command_error(lambda: self.commands.preview_init(self.source))
        self.assert_command_error(lambda: self.commands.preview_init(Path("bad\x00path")))
        self.assert_command_error(lambda: self.commands.preview_init(self.root))

    def test_source_parent_symlink_and_same_byte_inode_replacement_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_command_error(lambda: self.commands.preview_init(alias / self.source.name))
        plan = self.commands.preview_init(self.source)
        other = self.root / "replacement.json"
        self.write_private(other, BEFORE)
        other.replace(self.source)
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.local.exists())

    def test_init_rejects_case_aliases_into_private_backups(self):
        self.paths.local.mkdir(mode=0o700)
        self.paths.backups.mkdir(mode=0o700)
        source = self.paths.backups / "prepared.json"
        self.write_private(source, BEFORE)
        alias = self.root / "CONFIG/LOCAL/BACKUPS/prepared.json"
        if not alias.exists():
            self.skipTest("requires case-insensitive filesystem")
        self.assert_command_error(lambda: self.commands.preview_init(alias))
        self.assertFalse(self.paths.history.exists())

    def test_init_accepts_a_caller_prepared_private_file_outside_history_and_backups(self):
        self.paths.local.mkdir(mode=0o700)
        source = self.paths.local / "caller-prepared.json"
        self.write_private(source, BEFORE)
        with forbid_writes():
            plan = self.commands.preview_init(source)
        self.assertEqual(plan.profile_sha256, digest(BEFORE))
        self.assertFalse(self.paths.profile.exists())

    def test_init_revalidates_source_after_git_gate_before_creating_state(self):
        plan = self.commands.preview_init(self.source)
        gate = self.commands.store._require_git
        def change_source(require_main):
            gate(require_main)
            self.write_private(self.source, AFTER)
        with patch.object(self.commands.store, "_require_git", side_effect=change_source):
            self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.local.exists())

    def test_init_fifo_source_fails_without_blocking(self):
        pipe = self.root / "prepared.pipe"
        os.mkfifo(pipe, 0o600)
        self.assert_command_error(lambda: self.commands.preview_init(pipe))
        self.assertFalse(self.paths.local.exists())

    @macos_apply
    def test_migration_reads_only_exact_legacy_path_and_is_exact_byte_idempotent(self):
        legacy = self.root / "config/apps.json"
        legacy.write_bytes(BEFORE)
        legacy.chmod(0o644)
        before = self.tree()
        with forbid_writes():
            plan = self.commands.preview_migration()
        self.assertEqual(self.tree(), before)
        result = self.commands.apply(plan)
        self.assertTrue(result.applied)
        self.assertEqual(legacy.read_bytes(), BEFORE)
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, BEFORE)
        before = self.tree()
        with forbid_writes():
            again = self.commands.preview_migration()
        self.assertEqual(again.action, "already-current")
        self.assertFalse(self.commands.apply(again).applied)
        self.assertEqual(self.tree(), before)
        legacy.write_bytes(BEFORE + b"\n")
        self.assert_command_error(self.commands.preview_migration)
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)

    def test_migration_does_not_fall_back_to_prepared_or_private_files(self):
        self.assert_command_error(self.commands.preview_migration)
        legacy = self.root / "config/apps.json"
        legacy.symlink_to(self.source)
        self.assert_command_error(self.commands.preview_migration)
        legacy.unlink()
        legacy.write_bytes(b"{}")
        self.assert_command_error(self.commands.preview_migration)

    @macos_apply
    def test_exact_migration_retry_creates_missing_backup_without_a_second_revision(self):
        from local_web_server.host_profile_backup import HostProfileBackupError
        legacy = self.root / "config/apps.json"
        legacy.write_bytes(BEFORE)
        plan = self.commands.preview_migration()
        with patch("local_web_server.host_commands.write_host_backup", side_effect=HostProfileBackupError("private failure")):
            self.assert_command_error(lambda: self.commands.apply(plan))
        original = self.store.revisions()
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(len(original), 1)
        self.assertIsNone(self.commands.status().last_backup_at)
        with forbid_writes():
            retry = self.commands.preview_migration()
        self.assertEqual(retry.action, "backup-required")
        result = self.commands.apply(retry)
        self.assertTrue(result.applied)
        self.assertIsNone(result.revision_id)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, BEFORE)
        self.assertEqual(self.store.revisions(), original)
        self.assertEqual(legacy.read_bytes(), BEFORE)
        self.assertIsNotNone(self.commands.status().last_backup_at)
        self.assertFalse(self.commands.apply(self.commands.preview_migration()).applied)

    def test_status_missing_and_valid_profile_are_read_only_redacted_fixed_metadata(self):
        with forbid_writes():
            missing = self.commands.status().as_dict()
        self.assertEqual(missing["transactionState"], "missing")
        self.assertIsNone(missing["profileSha256"])
        self.store.initialise(BEFORE)
        with forbid_writes():
            status = self.commands.status().as_dict()
        self.assertEqual(set(status), {"profilePath", "registrySchema", "profileSha256", "revisionId", "revisionCount", "lastBackupAt", "transactionState", "legacyFilePresent"})
        self.assertEqual(status["profilePath"], str(self.paths.profile))
        self.assertEqual(status["registrySchema"], 1)
        self.assertEqual(status["profileSha256"], digest(BEFORE))
        self.assertEqual(status["revisionCount"], 1)
        self.assertEqual(status["transactionState"], "clean")
        encoded = json.dumps(status)
        for secret in ("localhost", "/tmp/runtime", "apps", "policy", "port", "origin"):
            self.assertNotIn(secret, encoded.replace(str(self.paths.profile), ""))

    def test_missing_status_also_comes_from_one_store_owned_snapshot(self):
        captured = type(
            "StatusSnapshot",
            (),
            {"profile_bytes": None, "history_ids": (), "recovery": None},
        )()

        with (
            patch.object(
                self.commands.store,
                "read_status_snapshot",
                return_value=captured,
            ) as read_status_snapshot,
            patch.object(
                self.commands,
                "_absent",
                side_effect=AssertionError("caller inspected missing store state"),
            ) as absent,
        ):
            status = self.commands.status()

        self.assertEqual(status.transaction_state, "missing")
        self.assertIsNone(status.profile_sha256)
        read_status_snapshot.assert_called_once_with()
        absent.assert_not_called()

    def test_status_uses_one_store_snapshot_when_disk_state_changes_after_capture(self):
        self.store.initialise(BEFORE)
        self.write_private(self.paths.transaction, b"pending-marker")
        recovery = type(
            "Recovery",
            (),
            {
                "action": "clear-marker",
                "current_bytes": AFTER,
                "history_ids": ("a" * 64, "b" * 64),
            },
        )()
        captured = type(
            "StatusSnapshot",
            (),
            {
                "profile_bytes": AFTER,
                "history_ids": recovery.history_ids,
                "recovery": recovery,
            },
        )()

        with (
            patch.object(
                self.commands.store,
                "read_status_snapshot",
                create=True,
                return_value=captured,
            ) as read_status_snapshot,
            patch.object(
                self.commands.store, "inspect_recovery", return_value=recovery
            ) as inspect_recovery,
        ):
            status = self.commands.status()

        self.assertEqual(status.profile_sha256, digest(AFTER))
        self.assertEqual(status.revision_id, "b" * 64)
        self.assertEqual(status.revision_count, 2)
        self.assertEqual(status.transaction_state, "clear-marker")
        read_status_snapshot.assert_called_once_with()
        inspect_recovery.assert_not_called()

    def test_status_snapshot_serializes_a_concurrent_store_publication(self):
        self.store.initialise(BEFORE)
        captured = threading.Event()
        release = threading.Event()
        result = {}
        original = self.commands.store._chain

        def pause_after_capture(local, history, **kwargs):
            snapshot = original(local, history, **kwargs)
            captured.set()
            if not release.wait(2):
                raise AssertionError("status snapshot was not released")
            return snapshot

        def read_status():
            try:
                result["status"] = self.commands.status()
            except BaseException as error:
                result["error"] = error

        with patch.object(
            self.commands.store, "_chain", side_effect=pause_after_capture
        ):
            thread = threading.Thread(target=read_status)
            thread.start()
            self.assertTrue(captured.wait(2))
            try:
                with self.assertRaises(HostProfileStoreError):
                    self.store.publish_registration("plotter", BEFORE, AFTER)
            finally:
                release.set()
                thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["status"].profile_sha256, digest(BEFORE))
        self.assertEqual(self.store.read_current(), BEFORE)

    @macos_apply
    def test_backup_default_and_explicit_targets_are_no_clobber_and_status_tracks_default(self):
        self.store.initialise(BEFORE)
        result = self.commands.backup()
        self.assertEqual(result.plan.destination_label, "private-backups")
        self.assertEqual(result.plan.planned_modes, ("files=0600",))
        self.assertEqual(result.backup_path.parent, self.paths.backups)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, BEFORE)
        with forbid_writes():
            self.assertEqual(self.commands.status().last_backup_at, "2026-09-10T12:00:00Z")
        self.assert_command_error(self.commands.backup)
        explicit = self.root / "export.json"
        self.assertEqual(self.commands.backup(explicit).plan.destination_label, "explicit-backup")
        original = explicit.read_bytes()
        self.assert_command_error(lambda: self.commands.backup(explicit))
        self.assertEqual(explicit.read_bytes(), original)
        for unsafe in (self.paths.profile, self.paths.history / "output.json", self.root, self.root / "missing/output.json"):
            self.assert_command_error(lambda: self.commands.backup(unsafe))

    def test_apply_requires_clean_main_and_ignores_ambient_git_and_profile_overrides(self):
        plan = self.commands.preview_init(self.source)
        self.git("switch", "-qc", "feature")
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "elsewhere"), "LOCAL_WEB_REGISTRY": str(self.source)}):
            self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.local.exists())
        self.git("switch", "-q", "main")
        (self.root / "local_web_server/source.py").write_text("# dirty\n")
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertFalse(self.paths.local.exists())

    def test_recover_preview_does_not_write_and_apply_revalidates_exact_plan(self):
        self.store.initialise(BEFORE)
        atomic = self.store._atomic
        def interrupt(directory, temporary, destination, content):
            atomic(directory, temporary, destination, content)
            if destination == self.paths.transaction.name:
                raise KeyboardInterrupt
        with patch.object(self.store, "_atomic", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.store.publish_registration("plotter", BEFORE, AFTER)
        before = self.tree()
        with forbid_writes():
            plan = self.commands.preview_recovery()
        self.assertEqual(plan.action, "remove-untouched-marker")
        self.assertEqual(plan.revision_count, 1)
        self.assertEqual(self.tree(), before)
        self.write_private(self.paths.profile, AFTER)
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertTrue(self.paths.transaction.exists())
        self.write_private(self.paths.profile, BEFORE)
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertTrue(self.commands.apply(self.commands.preview_recovery()).applied)
        self.assertFalse(self.paths.transaction.exists())
        self.assertEqual(self.store.read_current(), BEFORE)

    def assert_recovery_preview_digests(self, stop, action, current, count):
        self.store.initialise(BEFORE)
        revision_id = digest(envelope(AFTER, digest(BEFORE), "registration", "plotter"))
        destination = {"marker": self.paths.transaction.name,
                       "revision": "00000000000000000002-" + revision_id + ".json",
                       "profile": self.paths.profile.name}[stop]
        atomic = self.store._atomic
        def interrupted(directory, temporary, name, content, **kwargs):
            atomic(directory, temporary, name, content, **kwargs)
            if name == destination:
                raise KeyboardInterrupt
        with patch.object(self.store, "_atomic", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.store.publish_registration("plotter", BEFORE, AFTER)
        marker = self.paths.transaction.read_bytes()
        before = self.tree()
        with forbid_writes():
            plan = self.commands.preview_recovery()
            public = plan.as_dict()
        self.assertEqual(self.tree(), before)
        self.assertEqual(public["action"], action)
        self.assertEqual(public["profileSha256"], digest(current))
        self.assertEqual(public["previousProfileSha256"], digest(BEFORE))
        self.assertEqual(public.get("candidateProfileSha256"), digest(AFTER))
        self.assertEqual(public["revisionId"], revision_id)
        self.assertEqual(public["revisionCount"], count)
        self.assertEqual(public["sourceSha256"], digest(marker))
        output = json.dumps(public, sort_keys=True, separators=(",", ":"))
        self.assertLess(len(output), 2048)
        for secret in (str(self.root), "localhost", "example.test", "/tmp/runtime", "plotter"):
            self.assertNotIn(secret, output)

    def test_untouched_recovery_preview_exposes_exact_current_previous_candidate_revision_marker_digests(self):
        self.assert_recovery_preview_digests("marker", "remove-untouched-marker", BEFORE, 1)

    def test_publish_recovery_preview_exposes_exact_current_previous_candidate_revision_marker_digests(self):
        self.assert_recovery_preview_digests("revision", "publish-candidate", BEFORE, 2)

    def test_clear_recovery_preview_exposes_exact_current_previous_candidate_revision_marker_digests(self):
        self.assert_recovery_preview_digests("profile", "clear-marker", AFTER, 2)

    def test_status_and_recovery_bound_invalid_or_oversized_pending_state(self):
        self.store.initialise(BEFORE)
        self.write_private(self.paths.transaction, b"private invalid details")
        for action in (self.commands.status, self.commands.preview_recovery):
            self.assert_command_error(action)
        with self.paths.transaction.open("wb") as handle:
            handle.truncate(128 * 1024 * 1024)
        for action in (self.commands.status, self.commands.preview_recovery):
            self.assert_command_error(action)
        self.assertTrue(self.paths.transaction.exists())

    def backup_source(self):
        self.store.initialise(BEFORE)
        self.store.publish_registration("plotter", BEFORE, AFTER)
        source = self.root / "input-backup.json"
        self.write_private(source, build_host_backup(self.paths, CLOCK).document_bytes)
        return source

    def test_restore_preview_fully_validates_source_and_never_writes(self):
        source = self.backup_source()
        with forbid_writes():
            plan = self.commands.preview_restore(source, replace=True)
        self.assertEqual(plan.operation, "restore")
        self.assertEqual(plan.profile_sha256, digest(AFTER))
        self.assertEqual(plan.source_sha256, digest(source.read_bytes()))
        self.assert_command_error(lambda: self.commands.preview_restore(source))
        self.write_private(source, b"invalid private backup bytes")
        before = self.tree()
        with forbid_writes():
            self.assert_command_error(lambda: self.commands.preview_restore(source, replace=True))
        self.assertEqual(self.tree(), before)

    @macos_apply
    def test_restore_replacement_backs_up_current_state_and_retains_local_history(self):
        source = self.backup_source()
        self.store.publish_registration("plotter", AFTER, BEFORE)
        original = self.store.revisions()
        plan = self.commands.preview_restore(source, replace=True)
        result = self.commands.apply(plan)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, BEFORE)
        self.assertEqual(self.store.revisions()[:-1], original)
        self.assertEqual(self.store.revisions()[-1].operation, "backup-restoration")
        self.assertEqual(self.store.read_current(), AFTER)
        self.assertEqual(parse_host_backup(source).profile_bytes, AFTER)

    @macos_apply
    def test_restore_existing_exact_profile_is_noop_without_new_revision(self):
        source = self.backup_source()
        before = self.tree()
        plan = self.commands.preview_restore(source, replace=True)
        self.assertEqual(plan.action, "already-current")
        result = self.commands.apply(plan)
        self.assertFalse(result.applied)
        self.assertEqual(self.tree(), before)
        self.assertEqual(len(self.store.revisions()), 2)

    @macos_apply
    def test_restore_absent_profile_installs_exact_chain_from_backup(self):
        source = self.backup_source()
        backup = parse_host_backup(source)
        # Clear only disposable fixture state, retaining the independently
        # prepared backup as the user-supplied restore source.
        self.paths.profile.unlink()
        for path in self.paths.history.iterdir():
            path.unlink()
        with forbid_writes():
            plan = self.commands.preview_restore(source)
        self.assertFalse(self.paths.profile.exists())
        result = self.commands.apply(plan)
        self.assertTrue(result.applied)
        self.assertEqual(self.store.read_current(), AFTER)
        self.assertEqual([item.envelope_bytes for item in self.store.revisions()], [item.content for item in backup.revisions])

    def test_restore_rechecks_source_profile_history_and_backup_before_mutation(self):
        source = self.backup_source()
        self.store.publish_registration("plotter", AFTER, BEFORE)
        plan = self.commands.preview_restore(source, replace=True)
        self.write_private(source, source.read_bytes() + b"\n")
        self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertEqual(self.store.read_current(), BEFORE)

    @macos_apply
    def test_failed_or_changed_safety_backup_prevents_replacement(self):
        source = self.backup_source()
        self.store.publish_registration("plotter", AFTER, BEFORE)
        plan = self.commands.preview_restore(source, replace=True)
        original = self.store.revisions()
        from local_web_server.host_profile_backup import write_host_backup
        def corrupt_backup(*args, **kwargs):
            destination = write_host_backup(*args, **kwargs)
            self.write_private(destination, b"bad private backup")
            return destination
        with patch("local_web_server.host_commands.write_host_backup", side_effect=corrupt_backup):
            self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(self.store.revisions(), original)

    @macos_apply
    def test_replacement_cannot_accept_same_profile_with_changed_chain_at_publication(self):
        source = self.backup_source()
        self.store.publish_registration("plotter", AFTER, BEFORE)
        plan = self.commands.preview_restore(source, replace=True)
        publish = self.commands.store.publish_backup_restoration
        other = HostProfileStore(self.paths, clock=lambda: CLOCK() + timedelta(minutes=1))
        def change_history(before, after, **kwargs):
            other.publish_registration("plotter", BEFORE, AFTER)
            other.publish_registration("plotter", AFTER, BEFORE)
            return publish(before, after, **kwargs)
        with patch.object(self.commands.store, "publish_backup_restoration", side_effect=change_history):
            self.assert_command_error(lambda: self.commands.apply(plan))
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(len(self.store.revisions()), 5)


class HostCliIntegrationTests(StoreFixture):
    # Real disposable repository plus CLI. The sole selection injection is a
    # test-controlled module default, never an environment or ordinary flag.
    def setUp(self):
        super().setUp()
        self.source = self.root / "prepared.json"
        self.write_private(self.source, BEFORE)

    def run_cli(self, argv):
        from local_web_server.cli import main
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("local_web_server.host_commands.DEFAULT_PLATFORM_REPOSITORY", self.root), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_status_json_and_human_output_hide_all_registry_values(self):
        private = canonical_json_bytes({"schemaVersion": 1, "host": "private-host.example",
            "runtimeRoot": "/private/runtime-marker", "apps": [{"id": "hidden-app",
            "repository": "/private/repository-marker", "autoDeploy": True,
            "port": 48763, "startCommand": ["python3", "private-service-command"]}]})
        self.store.initialise(private)
        for argv in (["host", "status"], ["host", "status", "--json"]):
            with forbid_writes():
                code, output, error = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertLess(len(output), 2000)
            for secret in ("hidden-app", "private-host", "runtime-marker", "repository-marker", "48763", "autoDeploy", "private-service-command", "startCommand"):
                self.assertNotIn(secret, output)
            if "--json" in argv:
                self.assertEqual(output, json.dumps(json.loads(output), sort_keys=True, separators=(",", ":")) + "\n")

    @macos_apply
    def test_cli_lifecycle_preview_and_explicit_apply_use_real_private_state(self):
        with forbid_writes():
            code, output, error = self.run_cli(["host", "init", "--from", str(self.source)])
        self.assertEqual(code, 0)
        self.assertFalse(self.paths.local.exists())
        self.assertNotIn(str(self.source), output)
        self.assertNotIn("localhost", output)
        code, output, error = self.run_cli(["host", "init", "--from", str(self.source), "--apply"])
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["applied"])
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)

    def test_cli_operational_failures_are_bounded_and_never_expose_exception_details(self):
        from local_web_server.host_profile import HostProfileError
        from local_web_server.host_profile_backup import HostProfileBackupError
        from local_web_server.host_profile_store import HostProfileStoreError
        for error_type in (HostProfileError, HostProfileBackupError, HostProfileStoreError, HostCommandError, OSError, ValueError, RuntimeError, TypeError):
            with patch("local_web_server.cli.HostCommands", side_effect=error_type("private-marker" * 1000)):
                code, output, error = self.run_cli(["host", "status"])
            self.assertEqual((code, output), (2, ""))
            self.assertLess(len(error), 100)
            self.assertNotIn("private-marker", error)

    def test_cli_default_selection_ignores_ambient_profile_and_git_overrides(self):
        self.store.initialise(BEFORE)
        with patch.dict(os.environ, {"GIT_DIR": "/bad/private-git", "GIT_WORK_TREE": "/bad/private-worktree", "LOCAL_WEB_REGISTRY": str(self.source), "LOCAL_WEB_PLATFORM_REPOSITORY": "/bad/private-platform"}):
            code, output, error = self.run_cli(["host", "status", "--json"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["profilePath"], str(self.paths.profile))

    @macos_apply
    def test_cli_failed_initial_backup_never_claims_success_and_migration_retry_recovers(self):
        from local_web_server.host_profile_backup import HostProfileBackupError
        (self.root / "config/apps.json").write_bytes(BEFORE)
        with patch("local_web_server.host_commands.write_host_backup", side_effect=HostProfileBackupError("private failure")):
            code, output, error = self.run_cli(["host", "migrate-registry", "--apply"])
        self.assertEqual((code, output), (2, ""))
        self.assertNotIn("private failure", error)
        code, output, error = self.run_cli(["host", "status", "--json"])
        self.assertEqual((code, error), (0, ""))
        status = json.loads(output)
        self.assertEqual(status["revisionCount"], 1)
        self.assertIsNone(status["lastBackupAt"])
        code, output, error = self.run_cli(["host", "migrate-registry", "--apply"])
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["applied"])
        self.assertIsNone(json.loads(output)["publishedRevisionId"])
        self.assertEqual(len(self.store.revisions()), 1)
        self.assertEqual(len(tuple(self.paths.backups.glob("local-web-host-*.json"))), 1)

    def test_cli_failed_init_backup_directs_to_explicit_backup_and_retains_valid_state(self):
        from local_web_server.host_profile_backup import HostProfileBackupError
        with patch("local_web_server.host_commands.write_host_backup", side_effect=HostProfileBackupError("private failure")):
            code, output, error = self.run_cli(["host", "init", "--from", str(self.source), "--apply"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("host backup", error)
        self.assertLess(len(error), 160)
        self.assertNotIn("private failure", error)
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(len(self.store.revisions()), 1)
        code, output, error = self.run_cli(["host", "status", "--json"])
        self.assertEqual((code, error), (0, ""))
        self.assertIsNone(json.loads(output)["lastBackupAt"])


if __name__ == "__main__":
    unittest.main()
