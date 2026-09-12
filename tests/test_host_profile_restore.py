import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from local_web_server.host_profile_store import HostProfileSnapshot, HostProfileStore, parse_revision
from tests.test_host_profile_store import StoreFixture, BEFORE, AFTER, CLOCK, envelope, digest
from tests.suites import acceptance


macos_restore = unittest.skipUnless(sys.platform == "darwin", "requires macOS descriptor-clone restore publication")


class RestoreStoreTests(StoreFixture):
    def candidate(self):
        return HostProfileSnapshot(AFTER, (
            parse_revision(envelope()),
            parse_revision(envelope(AFTER, digest(BEFORE), "registration", "plotter")),
        ))

    def boundary_candidate(self, *, count=10_000, profile_size=5 * 1024 * 1024):
        profile = BEFORE + b" " * (profile_size - len(BEFORE))
        revisions, previous = [parse_revision(envelope())], BEFORE
        for sequence in range(2, count + 1):
            content = profile if sequence == count else (AFTER if sequence % 2 == 0 else BEFORE)
            revisions.append(parse_revision(envelope(content, digest(previous), "registration", f"fixture-{sequence}")))
            previous = content
        return HostProfileSnapshot(profile, tuple(revisions))

    def crash_restore(self, destination):
        atomic = self.store._atomic
        def interrupted(directory, temporary, name, content, **kwargs):
            atomic(directory, temporary, name, content, **kwargs)
            if name == destination:
                raise KeyboardInterrupt
        with patch.object(self.store, "_atomic", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.store.restore_snapshot(self.candidate())

    @macos_restore
    def test_absent_restore_installs_exact_complete_chain_and_profile(self):
        candidate = self.candidate()
        self.assertEqual(self.store.restore_snapshot(candidate), candidate.revisions[-1].revision_id)
        self.assertEqual(self.store.read_current(), AFTER)
        self.assertEqual(self.store.revisions(), candidate.revisions)
        self.assertFalse(self.paths.transaction.exists())

    @acceptance
    @macos_restore
    def test_restore_supports_ten_thousand_revisions_and_a_five_mib_profile(self):
        candidate = self.boundary_candidate()
        self.assertEqual(self.store.restore_snapshot(candidate), candidate.revisions[-1].revision_id)
        restored = self.store.read_snapshot(max_profile_bytes=5 * 1024 * 1024,
            max_revision_bytes=8 * 1024 * 1024, max_revisions=10_000, max_total_bytes=64 * 1024 * 1024)
        self.assertEqual(restored, candidate)
        marker = next(self.paths.recovery_residue.rglob("marker.json")).read_bytes()
        self.assertEqual(len(marker), 8_810_782)
        self.assertFalse(self.paths.transaction.exists())
        self.assertIsNone(self.store.inspect_recovery())

    def test_restore_rejects_one_over_the_profile_and_revision_count_bounds_before_writes(self):
        for candidate in (self.boundary_candidate(count=10_001),
                          self.boundary_candidate(profile_size=5 * 1024 * 1024 + 1)):
            self.assert_store_error(lambda: self.store.restore_snapshot(candidate))
            self.assertFalse(self.paths.local.exists())

    @macos_restore
    def test_absent_restore_preserves_a_marker_created_at_the_publication_boundary(self):
        foreign = b"external private transaction"
        atomic = self.store._atomic
        def collide(directory, temporary, name, content, **kwargs):
            if name == self.paths.transaction.name:
                self.write_private(self.paths.transaction, foreign)
            return atomic(directory, temporary, name, content, **kwargs)
        with patch.object(self.store, "_atomic", side_effect=collide):
            self.assert_store_error(lambda: self.store.restore_snapshot(self.candidate()))
        self.assertEqual(self.paths.transaction.read_bytes(), foreign)
        self.assertFalse(self.paths.profile.exists())
        self.assertEqual(list(self.paths.history.iterdir()), [])

    @macos_restore
    def test_absent_restore_preserves_a_marker_substituted_after_final_inspection(self):
        inspect = self.store._inspect
        foreign = b"external private final marker"
        def substitute(local, history):
            result = inspect(local, history)
            if result is not None and result.action == "clear-marker":
                external = self.paths.local / "external-final-marker"
                self.write_private(external, foreign)
                external.replace(self.paths.transaction)
            return result
        with patch.object(self.store, "_inspect", side_effect=substitute):
            self.assert_store_error(lambda: self.store.restore_snapshot(self.candidate()))
        self.assert_foreign_bytes_retained(foreign)

    def assert_foreign_bytes_retained(self, content):
        retained = [path for path in self.paths.local.rglob("*")
                    if path.is_file() and not path.is_symlink() and path.read_bytes() == content]
        self.assertTrue(retained, "recovery deleted the substituted foreign file")

    @macos_restore
    def test_recovery_preserves_a_temporary_created_after_exact_plan_revalidation(self):
        self.crash_restore(self.paths.transaction.name)
        plan = self.store.inspect_recovery()
        foreign = b"external private temporary"
        verify = self.store._verify_anchor
        def substitute(local, history):
            verify(local, history)
            if not self.paths.profile_temporary.exists():
                self.write_private(self.paths.profile_temporary, foreign)
        with patch.object(self.store, "_verify_anchor", side_effect=substitute):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assert_foreign_bytes_retained(foreign)

    @macos_restore
    def test_recovery_preserves_a_revision_substituted_after_its_last_read(self):
        name = "00000000000000000001-" + digest(envelope()) + ".json"
        self.crash_restore(name)
        plan = self.store.inspect_recovery()
        foreign = b"external private revision"
        read, verify = self.store._read, self.store._verify_anchor
        armed = False
        def arm(local, history):
            nonlocal armed
            verify(local, history)
            armed = True
        def substitute(directory, selected, **kwargs):
            nonlocal armed
            content = read(directory, selected, **kwargs)
            if armed and selected == name:
                armed = False
                external = self.paths.local / "external-revision"
                self.write_private(external, foreign)
                external.replace(self.paths.history / name)
            return content
        with patch.object(self.store, "_verify_anchor", side_effect=arm), patch.object(self.store, "_read", side_effect=substitute):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assert_foreign_bytes_retained(foreign)

    @macos_restore
    def test_recovery_preserves_a_marker_substituted_after_cleanup(self):
        self.crash_restore(self.paths.profile.name)
        plan = self.store.inspect_recovery()
        foreign = b"external private marker"
        verify = self.store._verify_anchor
        calls = 0
        def substitute(local, history):
            nonlocal calls
            verify(local, history)
            calls += 1
            if calls == 2:
                external = self.paths.local / "external-marker"
                self.write_private(external, foreign)
                external.replace(self.paths.transaction)
        with patch.object(self.store, "_verify_anchor", side_effect=substitute):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assert_foreign_bytes_retained(foreign)

    @macos_restore
    def test_completed_recovery_retains_private_residue_without_a_pending_transaction(self):
        name = "00000000000000000001-" + digest(envelope()) + ".json"
        self.crash_restore(name)
        marker = self.paths.transaction.read_bytes()
        self.store.recover(self.store.inspect_recovery())
        batches = list((self.paths.local / ".host-profile-recovery").glob(digest(marker) + "-*"))
        self.assertEqual(len(batches), 1)
        retained = batches[0]
        self.assertTrue(retained.is_dir())
        self.assertEqual((retained / "marker.json").read_bytes(), marker)
        self.assertEqual((retained / name).read_bytes(), envelope())
        self.assertFalse(self.paths.transaction.exists())
        self.assertEqual(list(self.paths.history.iterdir()), [])
        self.assertIsNone(self.store.inspect_recovery())

    @macos_restore
    def test_quarantine_cleanup_resumes_after_an_atomic_move_without_deleting_retained_files(self):
        from local_web_server import host_profile_recovery
        name = "00000000000000000001-" + digest(envelope()) + ".json"
        self.crash_restore(name)
        marker = self.paths.transaction.read_bytes()
        self.write_private(self.paths.profile_temporary, AFTER)
        plan = self.store.inspect_recovery()
        move = host_profile_recovery._move_no_clobber
        def stop(source, source_name, target, target_name):
            move(source, source_name, target, target_name)
            if source_name == self.paths.profile_temporary.name:
                raise KeyboardInterrupt
        with patch.object(host_profile_recovery, "_move_no_clobber", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.store.recover(plan)
        self.assertEqual(self.paths.transaction.read_bytes(), marker)
        reopened = HostProfileStore(self.paths, clock=CLOCK)
        resumed = reopened.inspect_recovery()
        self.assertEqual(resumed.action, "remove-untouched-marker")
        with patch("os.unlink", side_effect=AssertionError("recovery must retain, not delete")):
            reopened.recover(resumed)
        self.assertIsNone(reopened.inspect_recovery())
        self.assertFalse(self.paths.profile.exists())
        self.assertEqual(list(self.paths.history.iterdir()), [])
        self.assert_foreign_bytes_retained(AFTER)

    def test_absent_restore_rejects_existing_or_invalid_candidate_without_overwrite(self):
        self.store.initialise(BEFORE)
        original = self.paths.profile.read_bytes()
        self.assert_store_error(lambda: self.store.restore_snapshot(self.candidate()))
        self.assertEqual(self.paths.profile.read_bytes(), original)
        self.assert_store_error(lambda: self.store.restore_snapshot(HostProfileSnapshot(BEFORE, self.candidate().revisions)))

    def test_replacement_revision_has_fixed_operation_no_app_id_and_preserves_history(self):
        first = self.store.initialise(BEFORE)
        revision_id = self.store.publish_backup_restoration(BEFORE, AFTER)
        revisions = self.store.revisions()
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0].revision_id, first)
        self.assertEqual(revisions[1].revision_id, revision_id)
        self.assertEqual(revisions[1].operation, "backup-restoration")
        self.assertIsNone(revisions[1].app_id)
        self.assertEqual(revisions[1].previous_registry_sha256, digest(BEFORE))
        self.assertIsNone(self.store.publish_backup_restoration(AFTER, AFTER))
        self.assertEqual(len(self.store.revisions()), 2)

    def test_replacement_revalidates_the_entire_expected_history_under_the_store_lock(self):
        self.store.initialise(BEFORE)
        expected = self.store.revisions()
        self.store.publish_registration("plotter", BEFORE, AFTER)
        self.store.publish_registration("plotter", AFTER, BEFORE)
        self.assert_store_error(lambda: self.store.publish_backup_restoration(
            BEFORE, AFTER, expected_revisions=expected))
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(len(self.store.revisions()), 3)

    @macos_restore
    def test_marker_only_and_partial_chain_restore_roll_back_exact_files(self):
        for destination in (self.paths.transaction.name, "00000000000000000001-" + digest(envelope()) + ".json"):
            self.crash_restore(destination)
            plan = self.store.inspect_recovery()
            self.assertEqual(plan.action, "remove-untouched-marker")
            self.assertIsNone(self.store.recover(plan))
            self.assertFalse(self.paths.profile.exists())
            self.assertFalse(self.paths.transaction.exists())
            self.assertEqual(list(self.paths.history.iterdir()), [])

    @macos_restore
    def test_complete_chain_and_complete_profile_restore_finish_deterministically(self):
        last = self.candidate().revisions[-1]
        self.crash_restore("00000000000000000002-" + last.revision_id + ".json")
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "publish-candidate")
        self.assertEqual(self.store.recover(plan), last.revision_id)
        self.assertEqual(self.store.read_current(), AFTER)

    @macos_restore
    def test_profile_published_before_crash_only_clears_marker(self):
        self.crash_restore(self.paths.profile.name)
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "clear-marker")
        self.store.recover(plan)
        self.assertEqual(self.store.revisions(), self.candidate().revisions)
        self.assertEqual(self.store.read_current(), AFTER)

    @macos_restore
    def test_partial_restore_blocks_unknown_changed_or_nonprefix_revisions(self):
        self.crash_restore("00000000000000000001-" + digest(envelope()) + ".json")
        plan = self.store.inspect_recovery()
        unknown = self.paths.history / "unknown.json"
        self.write_private(unknown, b"private external bytes")
        self.assert_store_error(self.store.inspect_recovery)
        self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual(unknown.read_bytes(), b"private external bytes")
        unknown.unlink()
        first = next(self.paths.history.iterdir())
        original = first.read_bytes()
        self.write_private(first, original + b"\n")
        self.assert_store_error(self.store.inspect_recovery)
        self.assert_store_error(lambda: self.store.recover(plan))
        self.write_private(first, original)
        second = self.candidate().revisions[-1]
        self.write_private(self.paths.history / ("00000000000000000002-" + second.revision_id + ".json"), second.envelope_bytes)
        first.unlink()
        self.assert_store_error(self.store.inspect_recovery)

    @macos_restore
    def test_restore_rollback_can_itself_be_interrupted_and_resumed(self):
        candidate = HostProfileSnapshot(BEFORE, self.candidate().revisions + (
            parse_revision(envelope(BEFORE, digest(AFTER), "registration", "plotter")),))
        atomic = self.store._atomic
        def interrupted(directory, temporary, name, content, **kwargs):
            atomic(directory, temporary, name, content, **kwargs)
            if name.startswith("00000000000000000002-"):
                raise KeyboardInterrupt
        with patch.object(self.store, "_atomic", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.store.restore_snapshot(candidate)
        plan = self.store.inspect_recovery()
        from local_web_server import host_profile_recovery
        move = host_profile_recovery._move_no_clobber
        def interrupt_cleanup(source, name, target, target_name):
            move(source, name, target, target_name)
            if name.startswith("00000000000000000002-"):
                raise KeyboardInterrupt
        with patch.object(host_profile_recovery, "_move_no_clobber", side_effect=interrupt_cleanup), self.assertRaises(KeyboardInterrupt):
            self.store.recover(plan)
        resumed = self.store.inspect_recovery()
        self.assertEqual(resumed.action, "remove-untouched-marker")
        self.store.recover(resumed)
        self.assertEqual(list(self.paths.history.iterdir()), [])
        self.assertFalse(self.paths.profile.exists())

    @macos_restore
    def test_absent_restore_does_not_clobber_an_external_profile_created_during_publication(self):
        atomic = self.store._atomic
        def external_profile(directory, temporary, name, content, **kwargs):
            if name == self.paths.profile.name:
                self.write_private(self.paths.profile, b"external private profile")
            return atomic(directory, temporary, name, content, **kwargs)
        with patch.object(self.store, "_atomic", side_effect=external_profile):
            self.assert_store_error(lambda: self.store.restore_snapshot(self.candidate()))
        self.assertEqual(self.paths.profile.read_bytes(), b"external private profile")
        self.assertTrue(self.paths.transaction.exists())
        self.assert_store_error(self.store.inspect_recovery)

    @macos_restore
    def test_recovery_does_not_clobber_a_profile_created_after_plan_revalidation(self):
        from local_web_server import host_profile_backup
        last = self.candidate().revisions[-1]
        self.crash_restore("00000000000000000002-" + last.revision_id + ".json")
        plan = self.store.inspect_recovery()
        publish = host_profile_backup._publish_from_fd
        def external_profile(descriptor, directory, name):
            if name == self.paths.profile.name:
                self.write_private(self.paths.profile, b"external private profile")
            return publish(descriptor, directory, name)
        with patch.object(host_profile_backup, "_publish_from_fd", side_effect=external_profile):
            self.assert_store_error(lambda: self.store.recover(plan))
        self.assertEqual(self.paths.profile.read_bytes(), b"external private profile")
        self.assertTrue(self.paths.transaction.exists())

    @macos_restore
    def test_real_process_exit_leaves_only_the_three_recoverable_restore_states(self):
        script = '''
import os, sys
from pathlib import Path
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore, HostProfileSnapshot, parse_revision
from tests.test_host_profile_store import BEFORE, AFTER, CLOCK, envelope, digest
store = HostProfileStore(HostProfilePaths.for_repository(Path(sys.argv[1])), clock=CLOCK)
candidate = HostProfileSnapshot(AFTER, (parse_revision(envelope()), parse_revision(envelope(AFTER, digest(BEFORE), "registration", "plotter"))))
atomic = store._atomic
def stop(directory, temporary, name, content, **kwargs):
    atomic(directory, temporary, name, content, **kwargs)
    if name == sys.argv[2]:
        os._exit(77)
store._atomic = stop
store.restore_snapshot(candidate)
'''
        cases = ((self.paths.transaction.name, "remove-untouched-marker"),
                 ("00000000000000000001-" + digest(envelope()) + ".json", "remove-untouched-marker"),
                 ("00000000000000000002-" + self.candidate().revisions[-1].revision_id + ".json", "publish-candidate"),
                 (self.paths.profile.name, "clear-marker"))
        for name, expected in cases:
            with self.subTest(state=expected, filename=name):
                result = subprocess.run((sys.executable, "-c", script, str(self.root), name),
                    cwd=self.root, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 77, result.stderr.decode())
                plan = self.store.inspect_recovery()
                self.assertEqual(plan.action, expected)
                self.store.recover(plan)
                self.assertFalse(self.paths.transaction.exists())
                if expected == "remove-untouched-marker":
                    self.assertFalse(self.paths.profile.exists())
                    self.assertEqual(list(self.paths.history.iterdir()), [])
                else:
                    self.assertEqual(self.store.read_current(), AFTER)
                    self.paths.profile.unlink()
                    for path in self.paths.history.iterdir():
                        path.unlink()

    @macos_restore
    def test_ancestor_displacement_after_profile_publish_retains_recovery_marker(self):
        displaced = self.root / "displaced-local"
        external = self.root / "external-directory"
        external.mkdir(mode=0o700)
        atomic = self.store._atomic
        def displace(directory, temporary, name, content, **kwargs):
            atomic(directory, temporary, name, content, **kwargs)
            if name == self.paths.profile.name:
                self.paths.local.rename(displaced)
                self.paths.local.symlink_to(external, target_is_directory=True)
        with patch.object(self.store, "_atomic", side_effect=displace):
            self.assert_store_error(lambda: self.store.restore_snapshot(self.candidate()))
        self.assertEqual(list(external.iterdir()), [])
        self.assertTrue((displaced / self.paths.transaction.name).exists())
        self.assertEqual((displaced / "apps.json").read_bytes(), AFTER)
