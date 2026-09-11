import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from local_web_server.host_profile import HostProfilePaths, canonical_json_bytes
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError, HostProfileSnapshot, parse_revision


BEFORE = b'{ "schemaVersion": 1, "host": "localhost", "runtimeRoot": "/tmp/runtime", "apps": [] }\n'
AFTER = BEFORE.replace(b'localhost', b'example.test')
CLOCK = lambda: datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def envelope(content=BEFORE, previous=None, operation="initialisation", app_id=None):
    return canonical_json_bytes({
        "schema": "local-web-host-revision/v1", "appId": app_id,
        "createdAt": "2026-09-10T12:00:00Z", "operation": operation,
        "previousRegistrySha256": previous, "currentRegistrySha256": digest(content),
        "registryBase64": base64.b64encode(content).decode("ascii"),
    })


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-q", "-b", "main")
        (self.root / "config").mkdir()
        (self.root / "local_web_server").mkdir()
        (self.root / "local_web_server/source.py").write_text("# committed\n")
        (self.root / ".gitignore").write_text("config/local/\n")
        self.git("add", ".")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture")
        self.paths = HostProfilePaths.for_repository(self.root)
        self.store = HostProfileStore(self.paths, clock=CLOCK)

    def git(self, *arguments):
        return subprocess.run(["/usr/bin/git", "-C", str(self.root), *arguments], check=True, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout

    def write_private(self, path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    def assert_store_error(self, action):
        with self.assertRaises(HostProfileStoreError) as raised:
            action()
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertLessEqual(len(str(raised.exception)), 160)


class RevisionTests(StoreFixture):
    def test_initialisation_fails_before_writes_when_private_paths_are_not_ignored(self):
        for ignore_content in (None, "node_modules/\n"):
            with self.subTest(ignore_content=ignore_content):
                ignore = self.root / ".gitignore"
                if ignore_content is None:
                    ignore.unlink()
                else:
                    ignore.write_text(ignore_content, encoding="utf-8")

                self.assert_store_error(lambda: self.store.initialise(BEFORE))

                self.assertFalse(self.paths.local.exists())
                self.assertFalse(self.paths.profile.exists())
                self.assertFalse(self.paths.transaction.exists())
                self.git("restore", ".gitignore")

    def test_profile_changes_fail_without_mutation_when_ignore_contract_changes(self):
        self.store.initialise(BEFORE)
        profile_before = self.paths.profile.read_bytes()
        revisions_before = tuple(
            (path.name, path.read_bytes())
            for path in sorted(self.paths.history.iterdir())
        )
        (self.root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

        self.assert_store_error(
            lambda: self.store.publish_registration("plotter", BEFORE, AFTER)
        )

        self.assertEqual(self.paths.profile.read_bytes(), profile_before)
        self.assertEqual(
            tuple(
                (path.name, path.read_bytes())
                for path in sorted(self.paths.history.iterdir())
            ),
            revisions_before,
        )
        self.assertFalse(self.paths.transaction.exists())

    def test_reviewed_local_exclude_can_protect_old_main_without_git_mutation(self):
        (self.root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        exclude = self.root / ".git/info/exclude"
        before = exclude.read_bytes()
        reviewed = before + b"\nconfig/local/\n"
        exclude.write_bytes(reviewed)

        revision = self.store.initialise(BEFORE)

        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(self.store.revisions()[-1].revision_id, revision)
        self.assertEqual(exclude.read_bytes(), reviewed)

    def test_bounded_snapshot_returns_coherent_exact_validated_state(self):
        self.store.initialise(BEFORE)
        self.store.publish_registration("plotter", BEFORE, AFTER)
        snapshot = self.store.read_snapshot(max_profile_bytes=len(AFTER),
            max_revision_bytes=1000, max_revisions=2, max_total_bytes=3000)
        self.assertEqual(snapshot.profile_bytes, AFTER)
        self.assertEqual([revision.envelope_bytes for revision in snapshot.revisions],
                         [envelope(), envelope(AFTER, digest(BEFORE), "registration", "plotter")])

    def test_bounded_snapshot_enforces_profile_envelope_count_and_total_before_return(self):
        self.store.initialise(BEFORE)
        self.store.publish_registration("plotter", BEFORE, AFTER)
        limits = dict(max_profile_bytes=1000, max_revision_bytes=1000,
                      max_revisions=2, max_total_bytes=3000)
        for key, value in (("max_profile_bytes", 1), ("max_revision_bytes", 1),
                           ("max_revisions", 1), ("max_total_bytes", 1),
                           ("max_revisions", True), ("max_profile_bytes", -1)):
            self.assert_store_error(lambda: self.store.read_snapshot(**(limits | {key: value})))
        with self.paths.profile.open("wb") as handle:
            handle.truncate(5 * 1024 * 1024 + 1)
        self.assert_store_error(lambda: self.store.read_snapshot(**limits))

    def test_bounded_snapshot_validates_chain_and_profile_tip(self):
        self.store.initialise(BEFORE)
        limits = dict(max_profile_bytes=1000, max_revision_bytes=1000,
                      max_revisions=2, max_total_bytes=3000)
        self.write_private(self.paths.profile, AFTER)
        self.assert_store_error(lambda: self.store.read_snapshot(**limits))
        self.write_private(self.paths.profile, BEFORE)
        revision = next(self.paths.history.iterdir())
        revision.rename(self.paths.history / revision.name.replace("00000000000000000001", "00000000000000000002"))
        self.assert_store_error(lambda: self.store.read_snapshot(**limits))

    def test_bounded_snapshot_rejects_marker_and_publication_temporaries(self):
        self.store.initialise(BEFORE)
        limits = dict(max_profile_bytes=1000, max_revision_bytes=1000,
                      max_revisions=2, max_total_bytes=3000)
        for path in (
            self.paths.transaction,
            self.paths.profile_temporary,
            self.paths.revision_temporary,
            self.paths.transaction_temporary,
        ):
            with self.subTest(path=path.name):
                self.write_private(path, b"pending-private-state")
                self.assert_store_error(lambda: self.store.read_snapshot(**limits))
                path.unlink()

    def test_bounded_snapshot_rejects_oversized_and_deep_invalid_revisions(self):
        self.store.initialise(BEFORE)
        limits = dict(max_profile_bytes=5 * 1024 * 1024,
                      max_revision_bytes=8 * 1024 * 1024,
                      max_revisions=2, max_total_bytes=16 * 1024 * 1024)
        revision_path = next(self.paths.history.iterdir())
        with revision_path.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
        self.assert_store_error(lambda: self.store.read_snapshot(**limits))

        deep = b'{"x":' + b"[" * 2000 + b"]" * 2000 + b"}\n"
        deep_envelope = envelope(deep)
        revision_path.unlink()
        deep_path = self.paths.history / f"00000000000000000001-{digest(deep_envelope)}.json"
        self.write_private(deep_path, deep_envelope)
        self.write_private(self.paths.profile, deep)
        self.assert_store_error(lambda: self.store.read_snapshot(**limits))

    def test_initialisation_preserves_exact_bytes_in_canonical_envelope(self):
        revision_id = self.store.initialise(BEFORE)
        revisions = self.store.revisions()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].envelope_bytes, envelope())
        self.assertEqual(revision_id, digest(envelope()))
        self.assertEqual(revisions[0].registry_bytes, BEFORE)
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
        self.assertEqual(next(self.paths.history.iterdir()).name, f"00000000000000000001-{revision_id}.json")
        with self.assertRaises((AttributeError, TypeError)):
            revisions[0].registry_bytes = b"mutated"

    def test_parser_round_trips_registration_schema(self):
        content = envelope(AFTER, digest(BEFORE), "registration", "plotter")
        revision = parse_revision(content)
        self.assertEqual(revision.revision_id, digest(content))
        self.assertEqual(revision.registry_bytes, AFTER)
        self.assertEqual(revision.previous_registry_sha256, digest(BEFORE))
        self.assertEqual(revision.operation, "registration")
        self.assertEqual(revision.app_id, "plotter")

    def test_parser_rejects_malformed_noncanonical_or_untrusted_fields(self):
        good = json.loads(envelope())
        mutations = [
            {"operation": "user supplied message"}, {"appId": "plotter"},
            {"operation": "registration"}, {"registryBase64": "!!"},
            {"registryBase64": "Zg==\n"}, {"currentRegistrySha256": "0" * 64},
            {"previousRegistrySha256": "A" * 64}, {"unexpected": "private path"},
            {"createdAt": "not a timestamp"}, {"createdAt": "2026-99-99T12:00:00Z"},
            {"schema": "unknown"}, {"operation": ["registration"]},
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.assert_store_error(lambda: parse_revision(canonical_json_bytes(good | change)))
        for content in (envelope()[:-4], envelope() + b" ", b"[]", b"\xff", b'{"schema":1,"schema":2}'):
            self.assert_store_error(lambda: parse_revision(content))
        for app_id in (None, "../plotter", "Plotter", "x\n", 3):
            self.assert_store_error(lambda: parse_revision(envelope(AFTER, digest(BEFORE), "registration", app_id)))

    def test_chain_requires_continuity_unique_ids_and_current_tip(self):
        self.store.initialise(BEFORE)
        second = envelope(AFTER, digest(BEFORE), "registration", "plotter")
        second_path = self.paths.history / f"00000000000000000002-{digest(second)}.json"
        self.write_private(second_path, second)
        self.write_private(self.paths.profile, AFTER)
        self.assertEqual([r.registry_bytes for r in self.store.revisions()], [BEFORE, AFTER])
        self.write_private(self.paths.profile, BEFORE)
        self.assert_store_error(self.store.revisions)
        self.write_private(self.paths.profile, AFTER)
        duplicate = self.paths.history / f"00000000000000000003-{digest(second)}.json"
        self.write_private(duplicate, second)
        self.assert_store_error(self.store.revisions)
        duplicate.unlink()
        second_path.unlink()
        broken = envelope(AFTER, "0" * 64, "registration", "plotter")
        self.write_private(self.paths.history / f"00000000000000000002-{digest(broken)}.json", broken)
        self.assert_store_error(self.store.revisions)

    def test_chain_rejects_truncation_filename_mismatch_gaps_and_unknown_files(self):
        self.store.initialise(BEFORE)
        original = next(self.paths.history.iterdir())
        content = original.read_bytes()
        for name, data in ((original.name, content[:-1]), ("00000000000000000001-" + "0" * 64 + ".json", content), ("00000000000000000002-" + digest(content) + ".json", content), ("arbitrary.json", content)):
            original.unlink()
            original = self.paths.history / name
            self.write_private(original, data)
            self.assert_store_error(self.store.revisions)

    def test_invalid_registry_is_rejected_without_publishing_or_leaking(self):
        for content in (b"{}", b"secret invalid bytes", b'{"schemaVersion":1,"password":"private"}'):
            self.assert_store_error(lambda: self.store.initialise(content))
        self.assertFalse(self.paths.profile.exists())
        self.assertEqual(list(self.paths.history.iterdir()), [])
        self.assertEqual(list(self.paths.local.glob("tmp*")), [])

    def test_excessively_nested_envelope_fails_with_bounded_error(self):
        self.assert_store_error(lambda: parse_revision(b"[" * 3000 + b"0" + b"]" * 3000))

    def test_recovery_inspection_does_not_call_a_divergent_profile_clean(self):
        self.store.initialise(BEFORE)
        self.write_private(self.paths.profile, AFTER)
        self.assert_store_error(self.store.inspect_recovery)


class PublicationTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.tick = 0
        def clock():
            self.tick += 1
            return CLOCK() + timedelta(microseconds=self.tick)
        self.store = HostProfileStore(self.paths, clock=clock)

    def test_registry_validation_is_in_memory_and_never_creates_a_temporary(self):
        from local_web_server.config import load_registry
        observed = []
        def validate(path):
            self.assertFalse(isinstance(path, Path))
            observed.append(path)
            return load_registry(path)
        with patch("local_web_server.host_profile_store.load_registry", side_effect=validate):
            self.store.initialise(BEFORE)
        self.assertTrue(observed)
        self.assertFalse((self.paths.local / ".registry-validation.json.tmp").exists())

    def test_all_fixed_operations_publish_before_bytes_themselves(self):
        self.store.initialise(BEFORE)
        operations = (
            ("publish_registration", "registration"),
            ("publish_service_command_migration", "service-command-migration"),
            ("publish_service_command_restoration", "service-command-restoration"),
            ("publish_public_base_path_migration", "public-base-path-migration"),
            ("publish_public_base_path_restoration", "public-base-path-restoration"),
        )
        current = BEFORE
        for method, operation in operations:
            candidate = AFTER if current == BEFORE else BEFORE
            revision_id = getattr(self.store, method)("plotter", current, candidate)
            self.assertEqual(self.paths.profile.read_bytes(), candidate)
            last = self.store.revisions()[-1]
            self.assertEqual(last.operation, operation)
            self.assertEqual(last.revision_id, revision_id)
            current = candidate
        self.assertEqual(len(self.store.revisions()), 6)
        self.assertEqual(len({r.revision_id for r in self.store.revisions()}), 6)
        self.assertEqual(len(list(self.paths.history.iterdir())), 6)
        self.assertFalse(self.paths.transaction.exists())
        for directory in (self.paths.local, self.paths.history, self.paths.backups):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for file in directory.iterdir():
                if file.is_file():
                    self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_wrong_before_noop_and_duplicate_initialisation(self):
        self.store.initialise(BEFORE)
        self.assert_store_error(lambda: self.store.publish_registration("plotter", AFTER, BEFORE))
        self.assert_store_error(lambda: self.store.initialise(AFTER))
        self.assertIsNone(self.store.publish_registration("plotter", BEFORE, BEFORE))
        self.assertEqual(len(self.store.revisions()), 1)
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
        for app_id in (None, "../plotter", "Plotter"):
            self.assert_store_error(lambda: self.store.publish_registration(app_id, BEFORE, AFTER))

    def test_live_mutations_require_main_and_committed_installation_sources(self):
        self.store.initialise(BEFORE)
        self.git("switch", "-qc", "feature")
        self.store.require_clean(require_main=False)
        self.assert_store_error(lambda: self.store.require_clean(require_main=True))
        self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.git("switch", "-q", "main")
        source = self.root / "local_web_server/source.py"
        source.write_text("# dirty\n")
        self.assert_store_error(lambda: self.store.require_clean(require_main=False))
        self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        source.write_text("# committed\n")
        (self.root / "documentation.md").write_text("unrelated edit")
        self.store.require_clean(require_main=True)
        with patch.dict(os.environ, {"GIT_DIR": "/missing", "GIT_WORK_TREE": "/missing", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.status", "GIT_CONFIG_VALUE_0": "!false"}):
            self.store.require_clean(require_main=True)

    def crash_on_replace(self, target, *, after=False):
        original = os.replace
        def replace(source, destination, *args, **kwargs):
            if destination == target:
                if after:
                    original(source, destination, *args, **kwargs)
                raise OSError("injected private filesystem failure")
            return original(source, destination, *args, **kwargs)
        return patch("local_web_server.host_profile_store.os.replace", side_effect=replace)

    def test_failure_boundaries_offer_only_approved_recovery_states(self):
        self.store.initialise(BEFORE)
        for stage in ("before-marker", "after-marker", "after-revision", "after-profile", "before-remove"):
            with self.subTest(stage=stage):
                if stage == "before-marker":
                    failure = self.crash_on_replace(self.paths.transaction.name)
                elif stage == "after-marker":
                    failure = self.crash_on_replace(self.paths.transaction.name, after=True)
                elif stage == "after-revision":
                    original = os.replace
                    def replace(source, destination, *args, **kwargs):
                        original(source, destination, *args, **kwargs)
                        if source == self.paths.revision_temporary.name:
                            raise OSError("injected")
                    failure = patch("local_web_server.host_profile_store.os.replace", side_effect=replace)
                elif stage == "after-profile":
                    failure = self.crash_on_replace(self.paths.profile.name, after=True)
                else:
                    original_unlink = os.unlink
                    def unlink(name, *args, **kwargs):
                        if name == self.paths.transaction.name:
                            raise OSError("injected")
                        return original_unlink(name, *args, **kwargs)
                    failure = patch("local_web_server.host_profile_store.os.unlink", side_effect=unlink)
                with failure:
                    self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
                plan = self.store.inspect_recovery()
                if stage == "before-marker":
                    self.assertIsNone(plan)
                    self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
                    continue
                self.assertIsNotNone(plan)
                self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
                self.assert_store_error(lambda: self.store.require_clean(require_main=False))
                expected = {"after-marker": "remove-untouched-marker", "after-revision": "publish-candidate", "after-profile": "clear-marker", "before-remove": "clear-marker"}[stage]
                self.assertEqual(plan.action, expected)
                self.store.recover(plan)
                self.assertIsNone(self.store.inspect_recovery())
                wanted = BEFORE if stage == "after-marker" else AFTER
                self.assertEqual(self.store.read_current(), wanted)
                if wanted == AFTER:
                    self.store.publish_service_command_restoration("plotter", AFTER, BEFORE)

    def test_recovery_blocks_unapproved_state_and_stale_plan(self):
        self.store.initialise(BEFORE)
        with self.crash_on_replace(self.paths.transaction.name, after=True):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        plan = self.store.inspect_recovery()
        self.write_private(self.paths.profile, AFTER)
        self.assert_store_error(self.store.inspect_recovery)
        self.assert_store_error(lambda: self.store.recover(plan))
        self.write_private(self.paths.profile, BEFORE)
        marker = self.paths.transaction.read_bytes()
        self.write_private(self.paths.transaction, marker[:-1])
        self.assert_store_error(self.store.inspect_recovery)
        self.write_private(self.paths.transaction, marker)
        self.assert_store_error(lambda: self.store.recover(plan))
        self.store.recover(self.store.inspect_recovery())
        self.assert_store_error(lambda: self.store.recover(plan))

    def test_initialisation_crash_is_recoverable_without_an_existing_profile(self):
        with self.crash_on_replace(self.paths.profile.name):
            self.assert_store_error(lambda: self.store.initialise(BEFORE))
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "publish-candidate")
        self.store.recover(plan)
        self.assertEqual(self.store.read_current(), BEFORE)

    def test_secure_temporaries_fsync_and_readback_are_observable(self):
        self.store.initialise(BEFORE)
        real_replace, real_fsync = os.replace, os.fsync
        replacements, syncs = [], []
        def replace(source, destination, **kwargs):
            self.assertIsInstance(kwargs.get("src_dir_fd"), int)
            self.assertIsInstance(kwargs.get("dst_dir_fd"), int)
            self.assertNotIn("/", source)
            self.assertNotIn("/", destination)
            replacements.append((source, destination))
            return real_replace(source, destination, **kwargs)
        def fsync(fd):
            syncs.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real_fsync(fd)
        with patch("local_web_server.host_profile_store.os.replace", side_effect=replace), patch("local_web_server.host_profile_store.os.fsync", side_effect=fsync):
            self.store.publish_registration("plotter", BEFORE, AFTER)
        self.assertEqual([source for source, target in replacements], [self.paths.transaction_temporary.name, self.paths.revision_temporary.name, self.paths.profile_temporary.name])
        self.assertIn(True, syncs)
        self.assertIn(False, syncs)
        self.assertTrue(all(not path.exists() for path in self.paths.temporary_files))
        with self.crash_on_replace(self.paths.profile.name, after=True):
            self.assert_store_error(lambda: self.store.publish_service_command_restoration("plotter", AFTER, BEFORE))
        self.assertEqual(self.store.inspect_recovery().action, "clear-marker")

    def test_symlinks_modes_and_preexisting_temps_fail_closed(self):
        self.store.initialise(BEFORE)
        external = self.root / "external"
        external.write_bytes(b"do not touch")
        for path in (self.paths.profile_temporary, self.paths.revision_temporary, self.paths.transaction_temporary):
            path.symlink_to(external)
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
            self.assertEqual(external.read_bytes(), b"do not touch")
            path.unlink()
        revision = next(self.paths.history.iterdir())
        revision.chmod(0o640)
        self.assert_store_error(self.store.revisions)
        revision.chmod(0o600)
        self.paths.profile.chmod(0o640)
        self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.paths.profile.chmod(0o600)
        self.write_private(self.paths.profile_temporary, b"existing private temporary")
        self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.assertEqual(self.paths.profile_temporary.read_bytes(), b"existing private temporary")

    def test_in_memory_validation_cannot_publish_after_ancestor_swap(self):
        self.store.initialise(BEFORE)
        original_validate = self.store._validate_registry
        displaced = self.root / "displaced-local"
        external = self.root / "external-directory"
        external.mkdir(mode=0o700)
        swapped = False
        def validate(local, content):
            nonlocal swapped
            if not swapped:
                self.paths.local.rename(displaced)
                self.paths.local.symlink_to(external, target_is_directory=True)
                swapped = True
            return original_validate(local, content)
        with patch.object(self.store, "_validate_registry", side_effect=validate):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.assertTrue(swapped)
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual((displaced / "apps.json").read_bytes(), BEFORE)
        self.assertFalse((displaced / ".registry-validation.json.tmp").exists())

    def test_repository_ancestor_swap_cannot_redirect_publication(self):
        self.store.initialise(BEFORE)
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            parent = sandbox / "selected"
            external = sandbox / "external"
            selected_repository = parent / "repository"
            external_repository = external / "repository"
            shutil.copytree(self.root, selected_repository)
            shutil.copytree(self.root, external_repository)
            paths = HostProfilePaths.for_repository(selected_repository)
            store = HostProfileStore(paths, clock=CLOCK)
            initial_history = {
                file.name: file.read_bytes()
                for file in (external_repository / "config/local/history").iterdir()
            }
            original_open = os.open
            swapped = False
            def open_file(name, flags, *args, **kwargs):
                nonlocal swapped
                if name == paths.repository and flags & os.O_DIRECTORY and not swapped:
                    parent.rename(sandbox / "displaced")
                    parent.symlink_to(external, target_is_directory=True)
                    swapped = True
                return original_open(name, flags, *args, **kwargs)
            with patch("local_web_server.host_profile_store.os.open", side_effect=open_file):
                self.assert_store_error(lambda: store.publish_registration("plotter", BEFORE, AFTER))
            self.assertTrue(swapped)
            self.assertEqual((external_repository / "config/local/apps.json").read_bytes(), BEFORE)
            self.assertEqual({
                file.name: file.read_bytes()
                for file in (external_repository / "config/local/history").iterdir()
            }, initial_history)
            self.assertFalse((external_repository / "config/local/.host-profile-transaction.json").exists())

    def test_initialisation_checks_root_identity_before_creating_private_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            parent = sandbox / "selected"
            external = sandbox / "external"
            selected_repository = parent / "repository"
            external_repository = external / "repository"
            shutil.copytree(self.root, selected_repository)
            shutil.copytree(self.root, external_repository)
            paths = HostProfilePaths.for_repository(selected_repository)
            store = HostProfileStore(paths, clock=CLOCK)
            original_open = os.open
            swapped = False
            def open_file(name, flags, *args, **kwargs):
                nonlocal swapped
                if name == paths.repository and flags & os.O_DIRECTORY and not swapped:
                    parent.rename(sandbox / "displaced")
                    parent.symlink_to(external, target_is_directory=True)
                    swapped = True
                return original_open(name, flags, *args, **kwargs)
            with patch("local_web_server.host_profile_store.os.open", side_effect=open_file):
                self.assert_store_error(lambda: store.initialise(BEFORE))
            self.assertTrue(swapped)
            self.assertFalse((external_repository / "config/local").exists())

    def test_corrupt_profile_readback_keeps_marker_and_refuses_recovery(self):
        self.store.initialise(BEFORE)
        original = os.replace
        def replace(source, destination, **kwargs):
            original(source, destination, **kwargs)
            if destination == self.paths.profile.name:
                self.write_private(self.paths.profile, b"corrupted exact bytes")
        with patch("local_web_server.host_profile_store.os.replace", side_effect=replace):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.assertTrue(self.paths.transaction.exists())
        self.assertEqual(len(list(self.paths.history.iterdir())), 2)
        self.assert_store_error(self.store.inspect_recovery)

    def test_recovery_removes_matching_complete_staged_residue(self):
        self.store.initialise(BEFORE)
        with self.crash_on_replace(self.paths.transaction.name, after=True):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.write_private(self.paths.profile_temporary, AFTER)
        self.write_private(self.paths.transaction_temporary, self.paths.transaction.read_bytes())
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "remove-untouched-marker")
        self.store.recover(plan)
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertTrue(all(not path.exists() for path in self.paths.temporary_files))

    def test_process_exit_with_truncated_revision_temp_recovers_untouched_state(self):
        self.store.initialise(BEFORE)
        script = '''
import os
from pathlib import Path
import sys
from unittest.mock import patch
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from tests.test_host_profile_store import BEFORE, AFTER

store = HostProfileStore(HostProfilePaths.for_repository(Path(sys.argv[1])))
original_fdopen = os.fdopen

class InterruptedWrite:
    def __init__(self, handle):
        self.handle = handle
    def __enter__(self):
        return self
    def write(self, content):
        self.handle.write(content[:12])
        self.handle.flush()
        os.fsync(self.handle.fileno())
        os._exit(73)
    def __exit__(self, *args):
        self.handle.close()

def fdopen(descriptor, mode, **kwargs):
    handle = original_fdopen(descriptor, mode, **kwargs)
    if mode == "wb" and store.paths.revision_temporary.exists():
        actual = os.fstat(descriptor)
        staged = store.paths.revision_temporary.stat()
        if (actual.st_dev, actual.st_ino) == (staged.st_dev, staged.st_ino):
            return InterruptedWrite(handle)
    return handle

with patch("local_web_server.host_profile_store.os.fdopen", side_effect=fdopen):
    store.publish_registration("plotter", BEFORE, AFTER)
raise SystemExit(99)
'''
        process = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual(process.stdout, "")
        self.assertEqual(self.paths.revision_temporary.read_bytes(), b'{"appId":"pl')
        self.assertEqual(self.paths.profile.read_bytes(), BEFORE)
        self.assertTrue(self.paths.transaction.exists())
        complete_revisions = list(self.paths.history.glob("[0-9]*.json"))
        self.assertEqual(len(complete_revisions), 1)
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "remove-untouched-marker")
        self.assertIsNone(self.store.recover(plan))
        self.assertFalse(self.paths.revision_temporary.exists())
        self.assertFalse(self.paths.transaction.exists())
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(list(self.paths.history.iterdir()), complete_revisions)

    def test_unpublished_residue_recovery_still_rejects_wrong_types_modes_and_other_states(self):
        self.store.initialise(BEFORE)
        with self.crash_on_replace(self.paths.transaction.name, after=True):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        residue = self.paths.revision_temporary
        external = self.root / "external-residue"
        external.write_bytes(b"unpublished")
        residue.symlink_to(external)
        self.assert_store_error(self.store.inspect_recovery)
        residue.unlink()
        residue.mkdir(mode=0o700)
        self.assert_store_error(self.store.inspect_recovery)
        residue.rmdir()
        self.write_private(residue, b"truncated")
        residue.chmod(0o640)
        self.assert_store_error(self.store.inspect_recovery)
        residue.chmod(0o600)
        self.write_private(self.paths.profile, AFTER)
        self.assert_store_error(self.store.inspect_recovery)
        self.write_private(self.paths.profile, BEFORE)
        marker_bytes = self.paths.transaction.read_bytes()
        self.write_private(self.paths.transaction, marker_bytes[:-1])
        self.assert_store_error(self.store.inspect_recovery)
        self.write_private(self.paths.transaction, marker_bytes)
        plan = self.store.inspect_recovery()
        self.assertEqual(plan.action, "remove-untouched-marker")
        self.store.recover(plan)

        with self.crash_on_replace(self.paths.profile.name):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.write_private(residue, b"truncated")
        self.assert_store_error(self.store.inspect_recovery)
        self.write_private(self.paths.profile, AFTER)
        self.assert_store_error(self.store.inspect_recovery)

    def test_tampered_marker_digest_and_complete_revision_never_recover(self):
        self.store.initialise(BEFORE)
        with self.crash_on_replace(self.paths.profile.name):
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        marker_bytes = self.paths.transaction.read_bytes()
        for mutation in ({"candidateRegistrySha256": "0" * 64}, {"previousRegistryBase64": "!!"}, {"plannedRevisionId": "0" * 64}, {"unexpected": "field"}):
            self.write_private(self.paths.transaction, canonical_json_bytes(json.loads(marker_bytes) | mutation))
            self.assert_store_error(self.store.inspect_recovery)
        self.write_private(self.paths.transaction, marker_bytes)
        revision = sorted(self.paths.history.iterdir())[-1]
        self.write_private(revision, revision.read_bytes()[:-3])
        self.assert_store_error(self.store.inspect_recovery)

    def test_directory_lock_prevents_concurrent_publication(self):
        import fcntl
        self.store.initialise(BEFORE)
        descriptor = os.open(self.paths.local, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        finally:
            os.close(descriptor)
        self.assertEqual(self.store.read_current(), BEFORE)

    def test_duplicate_envelope_is_refused_without_a_new_marker(self):
        self.store = HostProfileStore(self.paths, clock=CLOCK)
        self.store.initialise(BEFORE)
        self.store.publish_registration("plotter", BEFORE, AFTER)
        self.store.publish_service_command_restoration("plotter", AFTER, BEFORE)
        self.assert_store_error(lambda: self.store.publish_registration("plotter", BEFORE, AFTER))
        self.assertEqual(len(self.store.revisions()), 3)
        self.assertFalse(self.paths.transaction.exists())


if __name__ == "__main__":
    unittest.main()
