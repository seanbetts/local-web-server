import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server import host_profile_recovery as recovery
from local_web_server.host_commands import HostCommands, HostCommandError
from local_web_server.host_profile_backup import build_host_backup, parse_host_backup
from local_web_server.host_profile_store import HostProfileSnapshot, HostProfileStore, parse_revision
from tests.test_host_profile_store import StoreFixture, BEFORE, AFTER, CLOCK, digest, envelope


@unittest.skipUnless(sys.platform == "darwin", "requires macOS no-clobber recovery retirement")
class RecoveryArchiveTests(StoreFixture):
    def start(self, stop="revision"):
        candidate = HostProfileSnapshot(AFTER, (parse_revision(envelope()),
            parse_revision(envelope(AFTER, digest(BEFORE), "registration", "plotter"))))
        first = "00000000000000000001-" + digest(envelope()) + ".json"
        last = "00000000000000000002-" + candidate.revisions[-1].revision_id + ".json"
        destination = {"marker": self.paths.transaction.name, "revision": first,
                       "complete": last, "profile": self.paths.profile.name}[stop]
        atomic = self.store._atomic
        def interrupted(directory, temporary, name, content, **kwargs):
            atomic(directory, temporary, name, content, **kwargs)
            if name == destination:
                raise KeyboardInterrupt
        with patch.object(self.store, "_atomic", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.store.restore_snapshot(candidate)
        return first

    def retained_bytes(self):
        return [path.read_bytes() for path in self.paths.local.rglob("*")
                if path.is_file() and not path.is_symlink()]

    def assert_boundary_substitution(self, role):
        first = self.start("profile" if role == "marker" else "revision")
        self.write_private(self.paths.profile_temporary, AFTER)
        selected = {"revision": first, "temporary": self.paths.profile_temporary.name,
                    "marker": self.paths.transaction.name}[role]
        foreign = ("foreign private " + role).encode()
        plan, move = self.store.inspect_recovery(), recovery._move_no_clobber
        def substitute(source, name, target, target_name):
            if name == selected:
                path = self.paths.history / name if role == "revision" else self.paths.local / name
                external = self.paths.local / "external-substitution"
                self.write_private(external, foreign)
                external.replace(path)
            move(source, name, target, target_name)
        with patch.object(recovery, "_move_no_clobber", side_effect=substitute):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertIn(foreign, self.retained_bytes())
        self.assert_store_error(self.store.inspect_recovery)
        with self.assertRaises(HostCommandError):
            HostCommands(self.root, clock=CLOCK).status()

    def test_revision_substituted_at_atomic_move_is_retained_and_blocks_recovery(self):
        self.assert_boundary_substitution("revision")

    def test_temporary_substituted_at_atomic_move_is_retained_and_blocks_recovery(self):
        self.assert_boundary_substitution("temporary")

    def test_marker_substituted_at_atomic_move_is_retained_and_never_reports_clean(self):
        self.assert_boundary_substitution("marker")

    def test_quarantine_destination_collision_preserves_both_files(self):
        first = self.start()
        original, foreign = envelope(), b"foreign private destination"
        plan, move = self.store.inspect_recovery(), recovery._move_no_clobber
        def collide(source, name, target, target_name):
            if name == first:
                descriptor = os.open(target_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(foreign)
            move(source, name, target, target_name)
        with patch.object(recovery, "_move_no_clobber", side_effect=collide):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual((self.paths.history / first).read_bytes(), original)
        self.assertIn(foreign, self.retained_bytes())
        self.assert_store_error(self.store.inspect_recovery)

    def test_interrupted_source_and_destination_duplicate_is_ambiguous_even_for_equal_bytes(self):
        first = self.start()
        plan, move = self.store.inspect_recovery(), recovery._move_no_clobber
        def duplicate(source, name, target, target_name):
            move(source, name, target, target_name)
            if name == first:
                self.write_private(self.paths.history / first, envelope())
                raise KeyboardInterrupt
        with patch.object(recovery, "_move_no_clobber", side_effect=duplicate), self.assertRaises(KeyboardInterrupt):
            self.store.recover(plan)
        self.assert_store_error(self.store.inspect_recovery)
        self.assertEqual(self.retained_bytes().count(envelope()), 2)

    def test_recovery_candidate_publication_never_unlinks_private_names(self):
        self.start("complete")
        plan = self.store.inspect_recovery()
        with patch("os.unlink", side_effect=AssertionError("recovery must never path-unlink")):
            self.store.recover(plan)
        self.assertEqual(self.store.read_current(), AFTER)
        self.assertIsNone(self.store.inspect_recovery())

    def test_interrupted_candidate_publication_reopens_as_clear_marker(self):
        self.start("complete")
        publish = recovery.RecoveryArchive._publish_profile
        def stop(archive, batch, plan):
            publish(archive, batch, plan)
            raise KeyboardInterrupt
        with patch.object(recovery.RecoveryArchive, "_publish_profile", stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        reopened = HostProfileStore(self.paths, clock=CLOCK)
        plan = reopened.inspect_recovery()
        self.assertEqual(plan.action, "clear-marker")
        with patch("os.unlink", side_effect=AssertionError("recovery must never path-unlink")):
            reopened.recover(plan)
        self.assertEqual(reopened.read_current(), AFTER)

    def test_interrupted_manifest_prefix_is_completed_without_replacing_its_inode(self):
        self.start()
        prefix = b""
        def stop(archive, batch, content):
            nonlocal prefix
            prefix = content[:len(content) // 2]
            descriptor = os.open("plan.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=batch)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(prefix)
            raise KeyboardInterrupt
        with patch.object(recovery.RecoveryArchive, "_publish_manifest", stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        staged = next(self.paths.recovery_residue.rglob("plan.tmp"))
        inode = staged.stat().st_ino
        from tests.test_host_commands import forbid_writes
        with forbid_writes():
            plan = self.store.inspect_recovery()
        self.assertEqual(staged.read_bytes(), prefix)
        self.store.recover(plan)
        self.assertEqual(staged.stat().st_ino, inode)
        self.assertTrue(staged.read_bytes().startswith(prefix))
        self.assertIsNone(self.store.inspect_recovery())

    def test_interrupted_candidate_prefix_resumes_without_deleting_its_staging_file(self):
        self.start("complete")
        def stop(archive, batch, plan):
            descriptor = os.open("candidate.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=batch)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(AFTER[:10])
            raise KeyboardInterrupt
        with patch.object(recovery.RecoveryArchive, "_publish_profile", stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        staged = next(self.paths.recovery_residue.rglob("candidate.tmp"))
        inode = staged.stat().st_ino
        plan = self.store.inspect_recovery()
        with patch("os.unlink", side_effect=AssertionError("recovery must never path-unlink")):
            self.store.recover(plan)
        self.assertEqual(staged.read_bytes(), AFTER)
        self.assertEqual(staged.stat().st_ino, inode)

    def assert_linked_partial_staging_is_not_appended(self, name):
        self.start("complete" if name == "candidate.tmp" else "revision")
        prefix = b""
        def stop(archive, batch, value):
            nonlocal prefix
            content = value.candidate_bytes if name == "candidate.tmp" else value
            prefix = content[:10]
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=batch)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(prefix)
            raise KeyboardInterrupt
        method = "_publish_profile" if name == "candidate.tmp" else "_publish_manifest"
        with patch.object(recovery.RecoveryArchive, method, stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        staged = next(self.paths.recovery_residue.rglob(name))
        alias = self.root / "outside-recovery-alias.json"
        os.link(staged, alias)
        identity = self.store._identity(staged.stat())
        self.assert_store_error(lambda: self.store.recover(self.store.inspect_recovery()))
        self.assertEqual(staged.read_bytes(), prefix)
        self.assertEqual(alias.read_bytes(), prefix)
        self.assertEqual(self.store._identity(staged.stat()), identity)
        self.assertEqual(self.store._identity(alias.stat()), identity)

    def test_hardlinked_plan_prefix_fails_without_mutating_either_alias(self):
        self.assert_linked_partial_staging_is_not_appended("plan.tmp")

    def test_hardlinked_candidate_prefix_fails_without_mutating_either_alias(self):
        self.assert_linked_partial_staging_is_not_appended("candidate.tmp")

    def assert_new_staging_descriptor_with_alias_is_not_written(self, selected):
        self.start("complete" if selected == "candidate.tmp" else "revision")
        plan = self.store.inspect_recovery()
        alias = self.root / "new-staging-alias.json"
        real_open = os.open
        def link_after_open(name, flags, *args, **kwargs):
            descriptor = real_open(name, flags, *args, **kwargs)
            if name == selected and flags & os.O_CREAT:
                os.link(name, alias, src_dir_fd=kwargs["dir_fd"], follow_symlinks=False)
            return descriptor
        with patch("os.open", side_effect=link_after_open):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual(alias.read_bytes(), b"")
        staged = next(self.paths.recovery_residue.rglob(selected))
        self.assertEqual(staged.read_bytes(), b"")
        self.assertEqual(staged.stat().st_ino, alias.stat().st_ino)

    def test_plan_descriptor_linked_during_open_is_rejected_before_writing(self):
        self.assert_new_staging_descriptor_with_alias_is_not_written("plan.tmp")

    def test_candidate_descriptor_linked_during_open_is_rejected_before_writing(self):
        self.assert_new_staging_descriptor_with_alias_is_not_written("candidate.tmp")

    def test_unknown_file_in_interrupted_quarantine_is_retained_and_blocks_inspection(self):
        self.start()
        move = recovery._move_no_clobber
        def stop(source, name, target, target_name):
            move(source, name, target, target_name)
            raise KeyboardInterrupt
        with patch.object(recovery, "_move_no_clobber", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        batch = next(self.paths.recovery_residue.iterdir())
        unknown = batch / "unknown.json"
        self.write_private(unknown, b"unrelated private bytes")
        self.assert_store_error(self.store.inspect_recovery)
        self.assertEqual(unknown.read_bytes(), b"unrelated private bytes")

    def test_resuming_does_not_charge_retained_manifest_bytes_twice(self):
        first = self.start()
        self.write_private(self.paths.profile_temporary, AFTER)
        move = recovery._move_no_clobber
        def stop(source, name, target, target_name):
            move(source, name, target, target_name)
            if name == self.paths.profile_temporary.name:
                raise KeyboardInterrupt
        with patch.object(recovery, "_move_no_clobber", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(self.store.inspect_recovery())
        required = sum(path.stat().st_size for path in self.paths.recovery_residue.rglob("*") if path.is_file())
        required += (self.paths.history / first).stat().st_size + self.paths.transaction.stat().st_size
        plan = self.store.inspect_recovery()
        with patch.object(recovery, "_MAX_RETAINED_BYTES", required):
            self.store.recover(plan)
        self.assertIsNone(self.store.inspect_recovery())

    def test_absent_status_rejects_a_corrupt_retired_marker(self):
        self.start()
        self.store.recover(self.store.inspect_recovery())
        marker = next(self.paths.recovery_residue.rglob("marker.json"))
        self.write_private(marker, b"foreign private marker")
        with self.assertRaises(HostCommandError):
            HostCommands(self.root, clock=CLOCK).status()

    def test_revision_disappearing_after_its_read_has_a_bounded_failure(self):
        first = self.start()
        plan = self.store.inspect_recovery()
        prepare, read = recovery.RecoveryArchive._prepare, self.store._read
        active = False
        def arm(archive, token):
            nonlocal active
            active = True
            return prepare(archive, token)
        def disappear(directory, name, **kwargs):
            nonlocal active
            content = read(directory, name, **kwargs)
            if active and name == first:
                active = False
                (self.paths.history / first).unlink()
            return content
        with patch.object(recovery.RecoveryArchive, "_prepare", arm), patch.object(self.store, "_read", side_effect=disappear):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertTrue(self.paths.transaction.exists())

    def test_marker_disappearing_at_the_commit_boundary_has_a_bounded_failure(self):
        self.start("profile")
        plan, verify = self.store.inspect_recovery(), self.store._verify_anchor
        calls = 0
        def disappear(local, history):
            nonlocal calls
            verify(local, history)
            calls += 1
            if calls == 2:
                self.paths.transaction.unlink()
        with patch.object(self.store, "_verify_anchor", side_effect=disappear):
            self.assert_store_error(lambda: self.store.recover(plan))

    def test_a_new_temporary_at_marker_retirement_is_preserved_and_never_reports_success(self):
        self.start("profile")
        plan, move = self.store.inspect_recovery(), recovery._move_no_clobber
        foreign = b"foreign private late temporary"
        def substitute(source, name, target, target_name):
            if name == self.paths.transaction.name:
                self.write_private(self.paths.profile_temporary, foreign)
            move(source, name, target, target_name)
        with patch.object(recovery, "_move_no_clobber", side_effect=substitute):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual(self.paths.profile_temporary.read_bytes(), foreign)

    def test_retention_count_limit_preserves_the_next_pending_transaction(self):
        with patch.object(recovery, "_MAX_BATCHES", 2):
            for _ in range(2):
                self.start("marker")
                self.store.recover(self.store.inspect_recovery())
            self.start("marker")
            marker = self.paths.transaction.read_bytes()
            self.assert_store_error(lambda: self.store.recover(self.store.inspect_recovery()))
        self.assertEqual(self.paths.transaction.read_bytes(), marker)
        self.assertFalse(self.paths.profile.exists())
        self.assertEqual(len(list(self.paths.recovery_residue.iterdir())), 2)

    def test_retention_byte_limit_is_checked_before_retiring_any_candidate(self):
        first = self.start()
        marker = self.paths.transaction.read_bytes()
        plan = self.store.inspect_recovery()
        with patch.object(recovery, "_MAX_RETAINED_BYTES", 1):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual(self.paths.transaction.read_bytes(), marker)
        self.assertEqual((self.paths.history / first).read_bytes(), envelope())

    def test_completed_residue_is_private_and_excluded_from_backups_and_status(self):
        self.start("profile")
        self.store.recover(self.store.inspect_recovery())
        for path in self.paths.recovery_residue.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        status = HostCommands(self.root, clock=CLOCK).status()
        self.assertEqual(status.transaction_state, "clean")
        document = build_host_backup(self.paths, CLOCK).document_bytes
        backup = self.root / "retention-backup.json"
        self.write_private(backup, document)
        self.assertEqual(len(parse_host_backup(backup).revisions), 2)
        self.assertNotIn(b"plan.json", document)
        self.assertNotIn(b".host-profile-recovery", document)

    def test_real_process_exit_after_each_retirement_step_reopens_exact_prefixes(self):
        script = '''
import os, sys
from pathlib import Path
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from local_web_server import host_profile_recovery as recovery
store = HostProfileStore(HostProfilePaths.for_repository(Path(sys.argv[1])))
move = recovery._move_no_clobber
def stop(source, name, target, target_name):
    move(source, name, target, target_name)
    if name == sys.argv[2]:
        os._exit(77)
recovery._move_no_clobber = stop
store.recover(store.inspect_recovery())
'''
        for role in ("temporary", "revision", "marker"):
            with self.subTest(boundary=role):
                first = self.start()
                self.write_private(self.paths.profile_temporary, AFTER)
                selected = {"temporary": self.paths.profile_temporary.name,
                            "revision": first, "marker": self.paths.transaction.name}[role]
                result = subprocess.run((sys.executable, "-c", script, str(self.root), selected),
                    cwd=self.root, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                reopened = HostProfileStore(self.paths, clock=CLOCK)
                plan = reopened.inspect_recovery()
                if role == "marker":
                    self.assertIsNone(plan)
                else:
                    self.assertEqual(plan.action, "remove-untouched-marker")
                    reopened.recover(plan)
                self.assertFalse(self.paths.profile.exists())
                self.assertFalse(self.paths.transaction.exists())
                self.assertEqual(list(self.paths.history.iterdir()), [])
