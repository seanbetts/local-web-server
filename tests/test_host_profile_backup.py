import base64
import copy
import ctypes
import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import local_web_server.host_profile_backup as backup_module

from local_web_server.host_profile import canonical_json_bytes
from local_web_server.host_profile_backup import (
    HostProfileBackupError, MAX_PROFILE_BYTES,
    build_host_backup, parse_host_backup, write_host_backup,
)
from tests.test_host_profile_store import StoreFixture, BEFORE, AFTER, CLOCK, digest, envelope

macos_writer = unittest.skipUnless(sys.platform == "darwin", "requires macOS descriptor-clone publication")

def record(name, content):
    return {"filename": name, "sha256": digest(content),
            "base64": base64.b64encode(content).decode("ascii")}


class BackupTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.store.initialise(BEFORE)
        self.store.publish_registration("plotter", BEFORE, AFTER)
        self.source = self.root / "input.json"
        self.expected = {
            "schema": "local-web-host-backup/v1", "createdAt": "2026-09-10T12:00:00Z",
            "profile": record("apps.json", AFTER),
            "revisions": [record(path.name, path.read_bytes())
                          for path in sorted(self.paths.history.iterdir())],
        }

    def parse_value(self, value):
        self.write_private(self.source, canonical_json_bytes(value))
        return parse_host_backup(self.source)

    def assert_backup_error(self, action):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(HostProfileBackupError) as raised:
                action()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("localhost", str(raised.exception))
        self.assertNotIn("example.test", str(raised.exception))
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertLessEqual(len(str(raised.exception)), 160)

    def set_attribute(self, path, name):
        subprocess.run(["/usr/bin/xattr", "-w", name, "foreign ancillary payload", str(path)],
                       check=True, capture_output=True)

    def attribute_names(self, path):
        return subprocess.run(["/usr/bin/xattr", str(path)], check=True,
                              capture_output=True, text=True).stdout.splitlines()

    @macos_writer
    def test_reused_scratch_custom_xattr_and_resource_fork_are_not_exported(self):
        scratch = self.root / self.paths.backup_temporary.name
        self.write_private(scratch, b"old scratch")
        for name in ("org.local-web.foreign", "com.apple.ResourceFork"):
            self.set_attribute(scratch, name)
        self.assertTrue({"org.local-web.foreign", "com.apple.ResourceFork"}.issubset(self.attribute_names(scratch)))
        output = self.root / "out.json"
        write_host_backup(self.paths, output, CLOCK)
        self.assertTrue(set(self.attribute_names(output)) <= {"com.apple.provenance"})
        self.assertEqual(self.attribute_names(scratch), self.attribute_names(output))
        self.assertEqual(output.read_bytes(), canonical_json_bytes(self.expected))

    @macos_writer
    def test_cloned_final_ancillary_payload_is_detected_before_success(self):
        publish = backup_module._publish_from_fd
        for name in ("org.local-web.foreign", "com.apple.ResourceFork"):
            output = self.root / (name + ".json")
            def add_payload(source, directory, destination):
                publish(source, directory, destination)
                self.set_attribute(output, name)
            with patch.object(backup_module, "_publish_from_fd", side_effect=add_payload):
                self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
            self.assertTrue(output.exists(), "must exercise post-clone metadata validation")

    @macos_writer
    def test_scratch_acl_and_flags_are_rejected_before_truncation(self):
        scratch = self.root / self.paths.backup_temporary.name
        self.write_private(scratch, b"old scratch")
        os.chflags(scratch, stat.UF_NODUMP)
        self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "flags.json", CLOCK))
        self.assertEqual(scratch.read_bytes(), b"old scratch")
        os.chflags(scratch, 0)
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(scratch)], check=True, capture_output=True)
        self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "acl.json", CLOCK))
        self.assertEqual(scratch.read_bytes(), b"old scratch")

    @macos_writer
    def test_final_flags_acl_and_multiple_links_are_rejected(self):
        publish = backup_module._publish_from_fd
        for mutation in ("flags", "acl", "links"):
            output = self.root / (mutation + ".json")
            def mutate_final(source, directory, destination):
                publish(source, directory, destination)
                if mutation == "flags":
                    os.chflags(output, stat.UF_NODUMP)
                elif mutation == "acl":
                    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(output)], check=True, capture_output=True)
                else:
                    os.link(output, self.root / "other-final-name")
            with patch.object(backup_module, "_publish_from_fd", side_effect=mutate_final):
                self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
            self.assertTrue(output.exists(), "must exercise post-clone metadata validation")

    @macos_writer
    def test_xattr_removal_failure_is_bounded_and_does_not_publish(self):
        scratch = self.root / self.paths.backup_temporary.name
        self.write_private(scratch, b"old scratch")
        self.set_attribute(scratch, "org.local-web.foreign")
        with patch.object(backup_module, "_remove_xattr", side_effect=OSError("private ancillary failure")):
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "out.json", CLOCK))
        self.assertFalse((self.root / "out.json").exists())
        self.assertEqual(scratch.read_bytes(), b"old scratch")

    @macos_writer
    def test_unavailable_metadata_api_and_final_metadata_read_failure_fail_closed(self):
        with patch.object(backup_module, "_list_xattrs", side_effect=OSError("unsupported metadata")):
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "unavailable.json", CLOCK))
        self.assertFalse((self.root / "unavailable.json").exists())
        original = backup_module._list_xattrs
        output = self.root / "final.json"
        def fail_final(fd):
            if output.exists():
                raise OSError("cannot validate final metadata")
            return original(fd)
        with patch.object(backup_module, "_list_xattrs", side_effect=fail_final):
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))

    @macos_writer
    def test_scratch_and_final_owner_must_match_effective_user(self):
        uid = os.geteuid()
        with patch("os.geteuid", return_value=uid + 1):
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "wrong-source-owner.json", CLOCK))
        output = self.root / "wrong-final-owner.json"
        with patch("os.geteuid", side_effect=lambda: uid + 1 if output.exists() else uid):
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertTrue(output.exists(), "must exercise post-clone owner validation")

    @macos_writer
    def test_managed_provenance_cannot_be_replaced_and_is_preserved_exactly(self):
        scratch = self.root / self.paths.backup_temporary.name
        self.write_private(scratch, b"old scratch")
        if "com.apple.provenance" not in self.attribute_names(scratch):
            self.skipTest("filesystem does not attach managed provenance")
        before = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.provenance", str(scratch)],
                                check=True, capture_output=True).stdout
        descriptor = os.open(scratch, os.O_RDWR)
        try:
            setting = ctypes.CDLL(None, use_errno=True).fsetxattr
            setting.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
            setting.restype = ctypes.c_int
            untrusted = b"caller-controlled-provenance"
            for flags in (0, 4):  # ordinary set, then explicit XATTR_REPLACE
                setting(descriptor, b"com.apple.provenance", untrusted, len(untrusted), 0, flags)
        finally:
            os.close(descriptor)
        after = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.provenance", str(scratch)],
                               check=True, capture_output=True).stdout
        self.assertTrue(before == after, "normal caller must not replace managed provenance")
        output = self.root / "out.json"
        write_host_backup(self.paths, output, CLOCK)
        final = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.provenance", str(output)],
                               check=True, capture_output=True).stdout
        self.assertTrue(final == before, "final provenance must equal sanitized source exactly")

    @macos_writer
    def test_unremoved_unexpected_attribute_fails_closed(self):
        scratch = self.root / self.paths.backup_temporary.name
        self.write_private(scratch, b"old scratch")
        self.set_attribute(scratch, "org.local-web.foreign")
        removing = backup_module._remove_xattr
        def leave_custom(fd, name):
            if name != b"org.local-web.foreign":
                return removing(fd, name)
        with patch.object(backup_module, "_remove_xattr", side_effect=leave_custom):
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "out.json", CLOCK))
        self.assertFalse((self.root / "out.json").exists())

    @macos_writer
    def test_final_provenance_value_mismatch_and_oversized_value_are_refused(self):
        original = backup_module._read_xattr
        output = self.root / "out.json"
        def altered_final(fd, name):
            if output.exists():
                return b"different-provenance"
            return original(fd, name)
        with patch.object(backup_module, "_read_xattr", side_effect=altered_final):
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertTrue(output.exists(), "must exercise post-clone validation")
        with patch.object(backup_module, "_native", return_value=lambda *args: 257):
            with self.assertRaises(OSError):
                original(-1, b"com.apple.provenance")
        with patch.object(backup_module, "_native", return_value=lambda *args: 65537):
            with self.assertRaises(OSError):
                backup_module._list_xattrs(-1)

    def test_case_aliases_cannot_target_protected_directory_identities(self):
        alias = self.paths.config / "LOCAL"
        if not alias.exists() or not alias.samefile(self.paths.local):
            self.skipTest("requires a case-insensitive disposable filesystem")
        for parent, name in ((alias, "ordinary.json"),
                             (alias / "HISTORY", "ordinary.json"),
                             (alias, self.paths.transaction.name),
                             (alias, ".APPS.JSON.TMP"),
                             (alias, ".REGISTRY-VALIDATION.JSON.TMP"),
                             (alias / "HISTORY", ".REVISION.JSON.TMP"),
                             (alias / "BACKUPS", ".HOST-PROFILE-BACKUP.JSON.TMP")):
            with self.subTest(parent=parent.name, name=name):
                self.assert_backup_error(lambda: write_host_backup(self.paths, parent / name, CLOCK))
                self.assertFalse((parent / name).exists())
        self.assertEqual(self.paths.profile.read_bytes(), AFTER)

    def test_casefolded_protected_basenames_are_refused_outside_host_directories(self):
        for path in (self.paths.profile, self.paths.profile_temporary, self.paths.revision_temporary,
                     self.paths.transaction, self.paths.transaction_temporary,
                     self.paths.backup_temporary):
            output = self.root / path.name.upper()
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
            self.assertFalse(output.exists())

    @macos_writer
    def test_substitution_in_stat_to_publication_gap_never_publishes_foreign_bytes(self):
        output = self.root / "export.json"
        temporary = self.root / self.paths.backup_temporary.name
        publish = backup_module._publish_from_fd
        def swap_then_publish(source, directory, destination):
            temporary.unlink()
            self.write_private(temporary, b"foreign publication")
            return publish(source, directory, destination)
        with patch.object(backup_module, "_publish_from_fd", side_effect=swap_then_publish):
            write_host_backup(self.paths, output, CLOCK)
        self.assertEqual(output.read_bytes(), canonical_json_bytes(self.expected))
        self.assertEqual(temporary.read_bytes(), b"foreign publication")

    @macos_writer
    def test_substitution_at_old_cleanup_boundary_never_removes_foreign_entry(self):
        output = self.root / "export.json"
        temporary = self.root / self.paths.backup_temporary.name
        read = backup_module._read
        def swap_after_readback(directory, name, limit, **kwargs):
            result = read(directory, name, limit, **kwargs)
            if name == output.name:
                temporary.unlink()
                self.write_private(temporary, b"foreign cleanup entry")
            return result
        with patch.object(backup_module, "_read", side_effect=swap_after_readback):
            write_host_backup(self.paths, output, CLOCK)
        self.assertEqual(output.read_bytes(), canonical_json_bytes(self.expected))
        self.assertEqual(temporary.read_bytes(), b"foreign cleanup entry")

    @macos_writer
    def test_descriptor_publication_is_atomic_no_clobber_and_uses_anonymous_source(self):
        source = self.root / "owned-source"
        self.write_private(source, b"owned bytes")
        descriptor = os.open(source, os.O_RDONLY)
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        source.unlink()
        try:
            backup_module._publish_from_fd(descriptor, parent, "clone.json")
            self.assertEqual((self.root / "clone.json").read_bytes(), b"owned bytes")
            self.assertEqual(stat.S_IMODE((self.root / "clone.json").stat().st_mode), 0o600)
            with self.assertRaises(OSError):
                backup_module._publish_from_fd(descriptor, parent, "clone.json")
            self.assertEqual((self.root / "clone.json").read_bytes(), b"owned bytes")
        finally:
            os.close(parent)
            os.close(descriptor)

    def test_unsupported_descriptor_publication_fails_closed(self):
        with patch.object(backup_module.sys, "platform", "unsupported"):
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "out.json", CLOCK))
        self.assertFalse((self.root / "out.json").exists())

    @macos_writer
    def test_secure_scratch_is_reused_without_deletion_and_locked_against_second_writer(self):
        temporary = self.root / self.paths.backup_temporary.name
        self.write_private(temporary, b"old scratch")
        original_inode = temporary.stat().st_ino
        first = self.root / "first.json"
        write_host_backup(self.paths, first, CLOCK)
        self.assertEqual(temporary.stat().st_ino, original_inode)
        self.assertEqual(temporary.read_bytes(), canonical_json_bytes(self.expected))
        descriptor = os.open(temporary, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "second.json", CLOCK))
            self.assertFalse((self.root / "second.json").exists())
        finally:
            os.close(descriptor)
        write_host_backup(self.paths, self.root / "second.json", CLOCK)
        self.assertEqual(temporary.stat().st_ino, original_inode)

    @macos_writer
    def test_substitution_immediately_after_open_keeps_owned_fd_and_foreign_entry(self):
        scratch = self.root / self.paths.backup_temporary.name
        original = backup_module._open_scratch
        def substitute(directory):
            descriptor = original(directory)
            scratch.unlink()
            self.write_private(scratch, b"foreign after open")
            return descriptor
        output = self.root / "out.json"
        with patch.object(backup_module, "_open_scratch", side_effect=substitute):
            write_host_backup(self.paths, output, CLOCK)
        self.assertEqual(output.read_bytes(), canonical_json_bytes(self.expected))
        self.assertEqual(scratch.read_bytes(), b"foreign after open")

    @macos_writer
    def test_two_cooperating_writers_cannot_truncate_the_locked_scratch(self):
        first, second = self.root / "first.json", self.root / "second.json"
        publish = backup_module._publish_from_fd
        def contend(source, directory, name):
            before = os.pread(source, 100000, 0)
            self.assert_backup_error(lambda: write_host_backup(self.paths, second, CLOCK))
            self.assertEqual(os.pread(source, 100000, 0), before)
            return publish(source, directory, name)
        with patch.object(backup_module, "_publish_from_fd", side_effect=contend):
            write_host_backup(self.paths, first, CLOCK)
        self.assertFalse(second.exists())
        self.assertEqual(first.read_bytes(), canonical_json_bytes(self.expected))

    def test_output_parent_protection_uses_open_identity_not_claimed_spelling(self):
        for actual in (self.paths.local, self.paths.history):
            descriptor = os.open(actual, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assert_backup_error(lambda: backup_module._validate_output_parent(
                    self.paths, descriptor, self.root / "unrelated.json"))
            finally:
                os.close(descriptor)

    def test_scratch_hardlink_is_refused_without_truncating_its_other_name(self):
        scratch = self.root / self.paths.backup_temporary.name
        os.link(self.paths.profile, scratch)
        self.assert_backup_error(lambda: write_host_backup(self.paths, self.root / "out.json", CLOCK))
        self.assertEqual(self.paths.profile.read_bytes(), AFTER)
        self.assertEqual(scratch.read_bytes(), AFTER)

    def test_canonical_deterministic_exact_bytes_and_stable_order(self):
        backup = build_host_backup(self.paths, CLOCK)
        self.assertEqual(backup.document_bytes, canonical_json_bytes(self.expected))
        self.assertEqual(backup.document_bytes, build_host_backup(self.paths, CLOCK).document_bytes)
        self.assertEqual(backup.profile_bytes, AFTER)
        self.assertEqual(backup.created_at, "2026-09-10T12:00:00Z")
        self.assertEqual([item.content for item in backup.revisions],
                         [envelope(), envelope(AFTER, digest(BEFORE), "registration", "plotter")])
        self.assertEqual(self.parse_value(self.expected), backup)
        self.assertNotIn("example.test", repr(backup))

    @macos_writer
    def test_default_and_explicit_output_are_private_and_round_trip(self):
        result = write_host_backup(self.paths, None, CLOCK)
        self.assertEqual(result.parent, self.paths.backups)
        self.assertEqual(result.name, "local-web-host-20260910T120000.000000Z.json")
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(parse_host_backup(result).document_bytes, canonical_json_bytes(self.expected))
        self.assert_backup_error(lambda: write_host_backup(self.paths, None, CLOCK))
        output = self.root / "export.json"
        with patch.dict(os.environ, {}, clear=False):
            old_umask = os.umask(0o777)
            try:
                self.assertEqual(write_host_backup(self.paths, output, CLOCK), output)
            finally:
                os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(parse_host_backup(output).profile_bytes, AFTER)
        self.assertEqual((output.parent / self.paths.backup_temporary.name).read_bytes(), canonical_json_bytes(self.expected))

    @macos_writer
    def test_default_publication_is_exclusive_complete_and_collision_does_not_touch_scratch(self):
        publish = backup_module._publish_from_fd
        def clone(source, directory, destination):
            self.assertEqual(os.pread(source, 100000, 0), canonical_json_bytes(self.expected))
            return publish(source, directory, destination)
        with patch.object(backup_module, "_publish_from_fd", side_effect=clone) as linking:
            output = write_host_backup(self.paths, None, CLOCK)
        self.assertEqual(linking.call_count, 1)
        self.write_private(self.paths.backup_temporary, b"stale partial temporary")
        self.assert_backup_error(lambda: write_host_backup(self.paths, None, CLOCK))
        self.assertEqual(self.paths.backup_temporary.read_bytes(), b"stale partial temporary")
        self.assertEqual(parse_host_backup(output).profile_bytes, AFTER)

    def test_unknown_missing_keys_and_record_types_are_refused(self):
        for target in ("root", "profile", "revision"):
            for operation in ("extra", "missing"):
                value = copy.deepcopy(self.expected)
                obj = value if target == "root" else value["profile"] if target == "profile" else value["revisions"][0]
                if operation == "extra":
                    obj["unexpected"] = "field"
                else:
                    obj.pop(next(iter(obj)))
                self.assert_backup_error(lambda: self.parse_value(value))
        for key, bad in (("schema", "v2"), ("profile", []), ("revisions", {}), ("revisions", [])):
            self.assert_backup_error(lambda: self.parse_value(self.expected | {key: bad}))

    def test_malformed_json_noncanonical_and_trailing_data_are_refused(self):
        good = canonical_json_bytes(self.expected)
        for content in (b"{", good + b"{}", good + b"\n", good[:-1], b"\xff", b"[]\n",
                        json.dumps(self.expected, indent=2).encode(),
                        good.replace(b'"schema":', b'"schema":"duplicate","schema":'),
                        b'{"schema":NaN}\n', b"[" * 2000):
            self.write_private(self.source, content)
            self.assert_backup_error(lambda: parse_host_backup(self.source))

    def test_timestamp_digest_and_strict_base64_validation(self):
        for created in (None, "2026-09-10", "2026-02-30T12:00:00Z", "2026-09-10T12:00:00+00:00"):
            self.assert_backup_error(lambda: self.parse_value(self.expected | {"createdAt": created}))
        self.assert_backup_error(lambda: build_host_backup(self.paths, lambda: datetime(2026, 9, 10)))
        for key, bad in (("sha256", "0" * 64), ("sha256", digest(AFTER).upper()),
                         ("sha256", None), ("base64", "!!"), ("base64", "Zh=="),
                         ("base64", None), ("base64", self.expected["profile"]["base64"] + "\n")):
            value = copy.deepcopy(self.expected)
            value["profile"][key] = bad
            self.assert_backup_error(lambda: self.parse_value(value))

    def test_exact_names_and_no_other_file_classes(self):
        for name in ("../apps.json", "/apps.json", "history/apps.json", "a\\apps.json", "env", "a" * 300):
            for target in ("profile", "revisions"):
                value = copy.deepcopy(self.expected)
                item = value["profile"] if target == "profile" else value["revisions"][0]
                item["filename"] = name
                self.assert_backup_error(lambda: self.parse_value(value))
        (self.paths.local / "secret.env").write_bytes(b"unrelated-private-value")
        (self.paths.backups / "host.git.bundle").write_bytes(b"unrelated-bundle")
        self.assertEqual(build_host_backup(self.paths, CLOCK).document_bytes, canonical_json_bytes(self.expected))
        self.write_private(self.paths.history / "extra.log", b"do not include")
        self.assert_backup_error(lambda: build_host_backup(self.paths, CLOCK))

    def test_chain_order_gaps_duplicates_and_tip_mismatch(self):
        for revisions in (list(reversed(self.expected["revisions"])),
                          [self.expected["revisions"][0]] * 2,
                          [self.expected["revisions"][1]]):
            self.assert_backup_error(lambda: self.parse_value(self.expected | {"revisions": revisions}))
        self.assert_backup_error(lambda: self.parse_value(self.expected | {"profile": record("apps.json", BEFORE)}))
        for content in (envelope(AFTER, "0" * 64, "registration", "plotter"), envelope(AFTER)):
            value = copy.deepcopy(self.expected)
            value["revisions"][1] = record(f"{2:020d}-{digest(content)}.json", content)
            self.assert_backup_error(lambda: self.parse_value(value))

    def test_invalid_decoded_registry_and_envelope_are_refused_without_writes(self):
        for content in (b'{"secret":"example.test"}', b"\xff", b"{}"):
            revision = envelope(content)
            value = self.expected | {"profile": record("apps.json", content),
                                    "revisions": [record(f"{1:020d}-{digest(revision)}.json", revision)]}
            self.write_private(self.source, canonical_json_bytes(value))
            with patch("os.open", wraps=os.open) as opening:
                self.assert_backup_error(lambda: parse_host_backup(self.source))
            self.assertTrue(all(not args[0][1] & (os.O_CREAT | os.O_WRONLY | os.O_RDWR)
                                for args in opening.call_args_list))
        value = copy.deepcopy(self.expected)
        value["revisions"][0] = record(value["revisions"][0]["filename"], b"{}")
        self.assert_backup_error(lambda: self.parse_value(value))

    def test_document_record_and_count_bounds(self):
        with self.source.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        self.source.chmod(0o600)
        self.assert_backup_error(lambda: parse_host_backup(self.source))
        revisions = [record(f"{1:020d}-{digest(envelope(AFTER))}.json", envelope(AFTER))]
        for sequence in range(2, 10_002):
            content = envelope(AFTER, digest(AFTER), "registration", f"app-{sequence}")
            revisions.append(record(f"{sequence:020d}-{digest(content)}.json", content))
        value = self.expected | {"revisions": revisions}
        self.assert_backup_error(lambda: self.parse_value(value))
        value = self.expected | {"profile": record("apps.json", b"x" * (5 * 1024 * 1024 + 1))}
        self.assert_backup_error(lambda: self.parse_value(value))
        value = self.expected | {"revisions": [record("a", b"x" * (8 * 1024 * 1024 + 1))]}
        self.assert_backup_error(lambda: self.parse_value(value))
        with self.paths.profile.open("wb") as handle:
            handle.truncate(MAX_PROFILE_BYTES + 1)
        self.assert_backup_error(lambda: build_host_backup(self.paths, CLOCK))

    def test_symlink_nonregular_and_insecure_source_or_output_refused(self):
        self.write_private(self.source, canonical_json_bytes(self.expected))
        link = self.root / "link.json"
        link.symlink_to(self.source)
        self.assert_backup_error(lambda: parse_host_backup(link))
        self.assert_backup_error(lambda: write_host_backup(self.paths, link, CLOCK))
        self.source.chmod(0o644)
        self.assert_backup_error(lambda: parse_host_backup(self.source))
        self.assert_backup_error(lambda: write_host_backup(self.paths, self.source, CLOCK))
        directory = self.root / "directory"
        directory.mkdir()
        self.assert_backup_error(lambda: parse_host_backup(directory))
        self.assert_backup_error(lambda: write_host_backup(self.paths, directory, CLOCK))
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        self.assert_backup_error(lambda: parse_host_backup(fifo))
        self.assert_backup_error(lambda: write_host_backup(self.paths, fifo, CLOCK))
        revision = sorted(self.paths.history.iterdir())[0]
        original = revision.read_bytes()
        revision.unlink()
        revision.symlink_to(self.source)
        self.assert_backup_error(lambda: build_host_backup(self.paths, CLOCK))
        self.assertEqual(self.source.read_bytes(), canonical_json_bytes(self.expected))
        revision.unlink()
        self.write_private(revision, original)

    def test_preexisting_temporary_and_host_state_targets_are_preserved(self):
        temporary = self.root / self.paths.backup_temporary.name
        self.write_private(temporary, b"existing")
        temporary.chmod(0o640)
        output = self.root / "export.json"
        self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertEqual(temporary.read_bytes(), b"existing")
        self.assertFalse(output.exists())
        temporary.unlink()
        temporary.symlink_to(self.paths.profile)
        self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertEqual(self.paths.profile.read_bytes(), AFTER)
        temporary.unlink()
        for target in (self.paths.profile, self.paths.transaction, self.paths.backup_temporary,
                       self.paths.history / "other.json"):
            self.assert_backup_error(lambda: write_host_backup(self.paths, target, CLOCK))

    @macos_writer
    def test_fsync_atomic_anchored_write_and_readback_detect_corruption(self):
        output = self.root / "export.json"
        publish, original_fsync = backup_module._publish_from_fd, os.fsync
        sync_types = []
        def fsync(fd):
            sync_types.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return original_fsync(fd)
        def clone(source, directory, destination):
            self.assertTrue(stat.S_ISREG(os.fstat(source).st_mode))
            self.assertEqual(destination, output.name)
            self.assertTrue(stat.S_ISDIR(os.fstat(directory).st_mode))
            return publish(source, directory, destination)
        with patch.object(backup_module, "_publish_from_fd", side_effect=clone), patch("os.fsync", side_effect=fsync):
            write_host_backup(self.paths, output, CLOCK)
        self.assertIn(True, sync_types)
        self.assertIn(False, sync_types)
        output.unlink()
        def corrupt(source, directory, destination):
            publish(source, directory, destination)
            self.write_private(output, b"corrupt")
        with patch.object(backup_module, "_publish_from_fd", side_effect=corrupt):
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))

    def test_symlinked_parent_and_ancestor_swap_cannot_redirect_write(self):
        parent = self.root / "selected"
        parent.mkdir()
        external = self.root / "external"
        external.mkdir()
        link = self.root / "linked-parent"
        link.symlink_to(external, target_is_directory=True)
        self.assert_backup_error(lambda: write_host_backup(self.paths, link / "export.json", CLOCK))
        original_open = os.open
        swapped = False
        def opening(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == self.paths.backup_temporary.name and flags & os.O_CREAT and not swapped:
                parent.rename(self.root / "displaced")
                parent.symlink_to(external, target_is_directory=True)
                swapped = True
            return original_open(name, flags, *args, **kwargs)
        with patch("os.open", side_effect=opening):
            self.assert_backup_error(lambda: write_host_backup(self.paths, parent / "export.json", CLOCK))
        self.assertTrue(swapped)
        self.assertEqual(list(external.iterdir()), [])

    @macos_writer
    def test_cleanup_does_not_remove_another_writers_temporary(self):
        output = self.root / "export.json"
        temporary = self.root / self.paths.backup_temporary.name
        publish = backup_module._publish_from_fd
        def clone(source, directory, destination):
            publish(source, directory, destination)
            temporary.unlink()
            self.write_private(temporary, b"another writer")
        with patch.object(backup_module, "_publish_from_fd", side_effect=clone):
            write_host_backup(self.paths, output, CLOCK)
        self.assertEqual(temporary.read_bytes(), b"another writer")

    @macos_writer
    def test_substituted_temporary_never_published_or_removed(self):
        output = self.root / "export.json"
        temporary = self.root / self.paths.backup_temporary.name
        original_fsync = os.fsync
        substituted = False
        def fsync(fd):
            nonlocal substituted
            result = original_fsync(fd)
            if not stat.S_ISDIR(os.fstat(fd).st_mode) and temporary.exists() and not substituted:
                temporary.unlink()
                self.write_private(temporary, b"another writer")
                substituted = True
            return result
        with patch("os.fsync", side_effect=fsync):
            write_host_backup(self.paths, output, CLOCK)
        self.assertEqual(output.read_bytes(), canonical_json_bytes(self.expected))
        self.assertEqual(temporary.read_bytes(), b"another writer")

    def test_valid_parse_is_read_only_and_embedded_prior_registry_is_validated(self):
        self.write_private(self.source, canonical_json_bytes(self.expected))
        with patch("os.open", wraps=os.open) as opening:
            self.assertEqual(parse_host_backup(self.source).profile_bytes, AFTER)
        self.assertTrue(all(not args[0][1] & (os.O_CREAT | os.O_WRONLY | os.O_RDWR)
                            for args in opening.call_args_list))
        invalid = b'{"schemaVersion":1,"host":"private-host","apps":[]}'
        initial = envelope(invalid)
        second = envelope(AFTER, digest(invalid), "registration", "plotter")
        value = self.expected | {"revisions": [record(f"{1:020d}-{digest(initial)}.json", initial),
                                               record(f"{2:020d}-{digest(second)}.json", second)]}
        self.assert_backup_error(lambda: self.parse_value(value))

    def test_write_failure_leaves_no_output_and_preserves_private_scratch(self):
        output = self.root / "export.json"
        original_fsync = os.fsync
        def fail_output_write(fd):
            if (self.root / self.paths.backup_temporary.name).exists():
                raise OSError("private-path must not escape")
            return original_fsync(fd)
        with patch("os.fsync", side_effect=fail_output_write):
            self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertFalse(output.exists())
        self.assertEqual(stat.S_IMODE((self.root / self.paths.backup_temporary.name).stat().st_mode), 0o600)

    def test_explicit_output_never_clobbers_existing_content_or_symlink_target(self):
        output = self.root / "export.json"
        self.write_private(output, b"existing private content")
        self.assert_backup_error(lambda: write_host_backup(self.paths, output, CLOCK))
        self.assertEqual(output.read_bytes(), b"existing private content")
        symlink = self.root / "symlink.json"
        symlink.symlink_to(output)
        self.assert_backup_error(lambda: write_host_backup(self.paths, symlink, CLOCK))
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(output.read_bytes(), b"existing private content")

    @macos_writer
    def test_default_filename_uses_utc_microseconds_and_four_digit_year(self):
        shifted = lambda: datetime(2026, 9, 10, 13, 2, 3, 123456, tzinfo=timezone(timedelta(hours=1)))
        output = write_host_backup(self.paths, None, shifted)
        self.assertEqual(output.name, "local-web-host-20260910T120203.123456Z.json")
        ancient = lambda: datetime(1, 1, 1, tzinfo=timezone.utc)
        output = write_host_backup(self.paths, None, ancient)
        self.assertEqual(output.name, "local-web-host-00010101T000000.000000Z.json")

    def test_profile_limit_accepts_exact_five_mebibytes(self):
        content = AFTER + b" " * (5 * 1024 * 1024 - len(AFTER))
        revision = envelope(content)
        value = self.expected | {"profile": record("apps.json", content),
                                "revisions": [record(f"{1:020d}-{digest(revision)}.json", revision)]}
        self.assertEqual(self.parse_value(value).profile_bytes, content)

    @macos_writer
    def test_process_exit_after_default_publication_leaves_only_complete_private_bytes(self):
        script = '''
import os
from pathlib import Path
import sys
from unittest.mock import patch
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_backup import write_host_backup
import local_web_server.host_profile_backup as backup_module
from tests.test_host_profile_store import CLOCK

publish = backup_module._publish_from_fd
def exit_after_clone(*args, **kwargs):
    publish(*args, **kwargs)
    os._exit(77)

with patch.object(backup_module, "_publish_from_fd", side_effect=exit_after_clone):
    write_host_backup(HostProfilePaths.for_repository(Path(sys.argv[1])), None, CLOCK)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(result.stdout, "")
        output = self.paths.backups / "local-web-host-20260910T120000.000000Z.json"
        self.assertEqual(parse_host_backup(output).document_bytes, canonical_json_bytes(self.expected))
        self.assertEqual(parse_host_backup(self.paths.backup_temporary).document_bytes,
                         canonical_json_bytes(self.expected))
        write_host_backup(self.paths, self.paths.backups / "other.json", CLOCK)
        self.assertEqual(self.paths.backup_temporary.read_bytes(), canonical_json_bytes(self.expected))
