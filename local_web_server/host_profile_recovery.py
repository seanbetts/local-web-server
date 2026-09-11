"""Bounded, no-delete retirement of exact private recovery candidates.

Darwin has no conditional-inode unlink. Move names without clobbering, verify
the moved inode and bytes, and retain every file, including mismatches. The
marker moves last; a durable manifest makes interrupted prefixes resumable.
"""

from contextlib import contextmanager
import ctypes
import os
import re
import stat

from .host_profile import canonical_json_bytes, profile_digest
from .host_profile_store import HostProfileStoreError, _object, _hex, _MAX_TOTAL, _MAX_REVISIONS


_ROOT = ".host-profile-recovery"
_SCHEMA = "local-web-host-recovery/v1"
_BATCH = re.compile(r"[0-9a-f]{64}-[0-9a-f]{64}\Z")
_MAX_BATCHES = 128
_MAX_RETAINED_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST = 4 * 1024 * 1024
_UNSAFE = "private host profile recovery residue is unsafe"


def _move_no_clobber(source, source_name, target, target_name):
    from .host_profile_backup import _native, HostProfileBackupError
    try:
        rename = _native("renameatx_np", ctypes.c_int,
                         (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint))
        if rename(source, os.fsencode(source_name), target, os.fsencode(target_name), 4):
            raise OSError(ctypes.get_errno(), "private recovery move failed")
    except HostProfileBackupError:
        raise HostProfileStoreError(_UNSAFE) from None


class RecoveryArchive:
    def __init__(self, store, local, history):
        self.store, self.local, self.history = store, local, history
        self.paths = store.paths
        self.temporaries = (self.paths.profile_temporary.name,
                            self.paths.transaction_temporary.name,
                            self.paths.revision_temporary.name)

    def _source(self, name):
        return self.history if name == self.paths.revision_temporary.name or name[0].isdigit() else self.local

    def _destination(self, name):
        return "marker.json" if name == self.paths.transaction.name else name

    def _stat(self, directory, name):
        try:
            return os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _file_identity(self, directory, name):
        metadata = self._stat(directory, name)
        if metadata is None:
            raise HostProfileStoreError(_UNSAFE)
        return self.store._identity(metadata)

    def _names(self, directory, limit):
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                if len(names) >= limit:
                    raise HostProfileStoreError(_UNSAFE)
                names.append(entry.name)
        return sorted(names)

    @contextmanager
    def _directory(self, parent, name, *, create=False):
        descriptor = self.store._open_directory(parent, name, create=create)
        try:
            yield descriptor
            current = self._stat(parent, name)
            owned = os.fstat(descriptor)
            if current is None or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise HostProfileStoreError(_UNSAFE)
        finally:
            os.close(descriptor)

    def _batch_name(self, marker_digest, identity):
        return marker_digest + "-" + profile_digest(canonical_json_bytes({"identity": list(identity)}))

    def _parse(self, content, batch_name):
        manifest = _object(content, {"schema", "markerSha256", "candidateSha256", "action", "historyIds", "files"})
        ids, files = manifest["historyIds"], manifest["files"]
        if (manifest["schema"] != _SCHEMA or not _hex(manifest["markerSha256"])
                or not _hex(manifest["candidateSha256"])
                or manifest["action"] not in ("remove-untouched-marker", "publish-candidate", "clear-marker")
                or not isinstance(ids, list) or len(ids) > _MAX_REVISIONS
                or any(not _hex(item) for item in ids) or len(set(ids)) != len(ids)
                or not isinstance(files, list) or not 1 <= len(files) <= _MAX_REVISIONS + 4):
            raise HostProfileStoreError(_UNSAFE)
        revisions = {self.store._filename(index, revision): revision for index, revision in enumerate(ids, 1)}
        revision_names = list(revisions)
        allowed = set(self.temporaries) | revisions.keys() | {self.paths.transaction.name}
        selected, seen = [], set()
        for record in files:
            if not isinstance(record, dict) or set(record) != {"name", "sha256", "identity"}:
                raise HostProfileStoreError(_UNSAFE)
            name, identity = record["name"], record["identity"]
            if (not isinstance(name, str) or name not in allowed or name in seen
                    or not _hex(record["sha256"]) or not isinstance(identity, list) or len(identity) != 7
                    or any(type(item) is not int or item < 0 for item in identity)
                    or not stat.S_ISREG(identity[2]) or stat.S_IMODE(identity[2]) != 0o600
                    or not 0 < identity[3] or identity[4] > _MAX_TOTAL):
                raise HostProfileStoreError(_UNSAFE)
            if name in revisions and record["sha256"] != revisions[name]:
                raise HostProfileStoreError(_UNSAFE)
            selected.append(name)
            seen.add(name)
        order = [name for name in self.temporaries if name in seen]
        archived_revisions = [name for name in reversed(revision_names) if name in seen]
        if archived_revisions and (manifest["action"] != "remove-untouched-marker"
                                   or len(archived_revisions) != len(revision_names)):
            raise HostProfileStoreError(_UNSAFE)
        order += archived_revisions + [self.paths.transaction.name]
        marker = files[-1]
        if (selected != order or marker["sha256"] != manifest["markerSha256"]
                or self._batch_name(marker["sha256"], marker["identity"]) != batch_name):
            raise HostProfileStoreError(_UNSAFE)
        return manifest

    def _match(self, directory, name, record, *, moved):
        content = self.store._read(directory, name, limit=_MAX_TOTAL)
        metadata = self._stat(directory, name)
        identity = self.store._identity(metadata) if metadata is not None else ()
        expected = tuple(record["identity"])
        # Rename changes ctime; device/inode/mode/link-count/size/mtime and
        # exact bytes must still match. Source identities also bind ctime.
        if (identity[:6] != expected[:6] or (not moved and identity != expected)
                or profile_digest(content) != record["sha256"]):
            raise HostProfileStoreError(_UNSAFE)
        return identity

    def _load(self, batch, name, plan=None):
        content = self.store._read(batch, "plan.json", optional=True, limit=_MAX_MANIFEST)
        staged = self.store._read(batch, "plan.tmp", optional=True, limit=_MAX_MANIFEST)
        if (content is None and staged is not None and plan is not None
                and self._names(batch, _MAX_REVISIONS + 6) == ["plan.tmp"]):
            # Before any move, the full manifest can be regenerated from the
            # exact canonical sources. Only its exact staged prefix is valid.
            expected = self._prepare(plan)
            if not expected.startswith(staged):
                raise HostProfileStoreError(_UNSAFE)
            return self._parse(expected, name), expected
        if content is None:
            content = staged
        if content is None:
            if self._names(batch, _MAX_REVISIONS + 6):
                raise HostProfileStoreError(_UNSAFE)
            return None, None
        if staged is not None and staged != content:
            raise HostProfileStoreError(_UNSAFE)
        manifest = self._parse(content, name)
        allowed = {"plan.json", "plan.tmp", "candidate.tmp", "candidate.publish"} | {
            self._destination(item["name"]) for item in manifest["files"]}
        if set(self._names(batch, _MAX_REVISIONS + 6)) - allowed:
            raise HostProfileStoreError(_UNSAFE)
        for candidate in ("candidate.tmp", "candidate.publish"):
            data = self.store._read(batch, candidate, optional=True, limit=_MAX_TOTAL)
            prefix = (candidate == "candidate.tmp" and plan is not None
                      and plan.action == "publish-candidate" and plan.candidate_bytes.startswith(data or b""))
            if data is not None and (manifest["action"] != "publish-candidate"
                                     or (profile_digest(data) != manifest["candidateSha256"] and not prefix)):
                raise HostProfileStoreError(_UNSAFE)
        return manifest, content

    def _inventory(self, root, pending_name, plan=None):
        names, total = self._names(root, _MAX_BATCHES), 0
        for name in names:
            if not _BATCH.fullmatch(name):
                raise HostProfileStoreError(_UNSAFE)
            with self._directory(root, name) as batch:
                for filename in self._names(batch, _MAX_REVISIONS + 6):
                    metadata = self._stat(batch, filename)
                    if (metadata is None or not stat.S_ISREG(metadata.st_mode)
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or (filename in ("plan.tmp", "candidate.tmp") and metadata.st_nlink != 1)):
                        raise HostProfileStoreError(_UNSAFE)
                    total += metadata.st_size
                    if total > _MAX_RETAINED_BYTES:
                        raise HostProfileStoreError(_UNSAFE)
                manifest, _ = self._load(batch, name, plan if name == pending_name else None)
                if name != pending_name:
                    if manifest is None:
                        raise HostProfileStoreError(_UNSAFE)
                    for record in manifest["files"]:
                        self._match(batch, self._destination(record["name"]), record, moved=True)
        return names, total

    def _pending(self, batch, manifest, plan):
        if plan is None:
            raise HostProfileStoreError(_UNSAFE)
        ids = tuple(manifest["historyIds"])
        revision_records = [item for item in manifest["files"] if item["name"][0].isdigit()]
        if (manifest["markerSha256"] != profile_digest(plan.marker_bytes)
                or manifest["candidateSha256"] != profile_digest(plan.candidate_bytes)
                or (manifest["action"] != plan.action
                    and (manifest["action"], plan.action) != ("publish-candidate", "clear-marker"))
                or (revision_records and (not plan.restore_records or ids[:len(plan.history_ids)] != plan.history_ids))
                or (not revision_records and ids != plan.history_ids)):
            raise HostProfileStoreError(_UNSAFE)
        if revision_records and ids != tuple(item[1] for item in plan.restore_records[:len(ids)]):
            raise HostProfileStoreError(_UNSAFE)
        states, waiting = [], False
        selected = {record["name"] for record in manifest["files"]}
        for temporary in self.temporaries:
            if temporary not in selected and self._stat(self._source(temporary), temporary) is not None:
                raise HostProfileStoreError(_UNSAFE)
        for record in manifest["files"]:
            source, target = self._source(record["name"]), self._destination(record["name"])
            at_source = self._stat(source, record["name"]) is not None
            at_target = self._stat(batch, target) is not None
            if at_source == at_target or (at_target and waiting):
                raise HostProfileStoreError(_UNSAFE)
            waiting |= at_source
            identity = self._match(source if at_source else batch,
                                   record["name"] if at_source else target, record, moved=at_target)
            states.append((record["name"], at_target, identity))
        return tuple(states)

    def inspect(self, plan):
        if self._stat(self.local, _ROOT) is None:
            return ()
        name = None if plan is None else self._batch_name(
            profile_digest(plan.marker_bytes), dict(plan.file_identities)[self.paths.transaction.name])
        with self._directory(self.local, _ROOT) as root:
            names, _ = self._inventory(root, name, plan)
            if name not in names:
                return (name, None, (), (), os.fstat(root).st_ino, None)
            with self._directory(root, name) as batch:
                manifest, content = self._load(batch, name, plan)
                states = self._pending(batch, manifest, plan) if manifest is not None else ()
                metadata = tuple((filename, self._file_identity(batch, filename))
                                 for filename in self._names(batch, _MAX_REVISIONS + 6))
                return (name, content, states, metadata, os.fstat(root).st_ino, os.fstat(batch).st_ino)

    def _prepare(self, plan):
        identities = dict(plan.file_identities)
        names = [name for name in self.temporaries if name in identities]
        if plan.restore_records and plan.action == "remove-untouched-marker":
            names.extend(self.store._filename(index, plan.history_ids[index - 1])
                         for index in range(len(plan.history_ids), 0, -1))
        names.append(self.paths.transaction.name)
        records = []
        for name in names:
            content = self.store._read(self._source(name), name, limit=_MAX_TOTAL)
            identity = self._file_identity(self._source(name), name)
            if identity != identities[name]:
                raise HostProfileStoreError(_UNSAFE)
            records.append({"name": name, "sha256": profile_digest(content), "identity": list(identity)})
        return canonical_json_bytes({"schema": _SCHEMA, "markerSha256": profile_digest(plan.marker_bytes),
            "candidateSha256": profile_digest(plan.candidate_bytes),
            "action": plan.action, "historyIds": list(plan.history_ids), "files": records})

    @contextmanager
    def _retained_file(self, batch, name, content, *, complete_prefix=False):
        staged = self._stat(batch, name)
        if staged is not None and staged.st_nlink != 1:
            raise HostProfileStoreError(_UNSAFE)
        flags = (os.O_RDWR | os.O_APPEND if complete_prefix else os.O_RDONLY) if staged is not None else os.O_RDWR | os.O_CREAT | os.O_EXCL
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=batch)
        try:
            if os.fstat(descriptor).st_nlink != 1:
                raise HostProfileStoreError(_UNSAFE)
            existing = b""
            if staged is not None:
                existing = self.store._read(batch, name, limit=_MAX_TOTAL)
                if (self.store._identity(os.fstat(descriptor)) != self.store._identity(staged)
                        or self.store._identity(os.fstat(descriptor)) != self._file_identity(batch, name)
                        or (existing != content and (not complete_prefix or not content.startswith(existing)))):
                    raise HostProfileStoreError(_UNSAFE)
            if staged is None or existing != content:
                if os.fstat(descriptor).st_nlink != 1:
                    raise HostProfileStoreError(_UNSAFE)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(content[len(existing):])
                    handle.flush()
                os.fsync(descriptor)
                os.fsync(batch)
            if self.store._read(batch, name, limit=_MAX_TOTAL) != content:
                raise HostProfileStoreError(_UNSAFE)
            if self.store._identity(os.fstat(descriptor)) != self._file_identity(batch, name):
                raise HostProfileStoreError(_UNSAFE)
            yield descriptor
        finally:
            os.close(descriptor)

    def _publish_manifest(self, batch, content):
        from .host_profile_backup import _publish_from_fd
        if self._stat(batch, "plan.json") is not None:
            if self.store._read(batch, "plan.json", limit=_MAX_MANIFEST) != content:
                raise HostProfileStoreError(_UNSAFE)
            return
        with self._retained_file(batch, "plan.tmp", content, complete_prefix=True) as descriptor:
            _publish_from_fd(descriptor, batch, "plan.json")
        os.fsync(batch)
        if self.store._read(batch, "plan.json", limit=_MAX_MANIFEST) != content:
            raise HostProfileStoreError(_UNSAFE)

    def _publish_profile(self, batch, plan):
        from .host_profile_backup import _publish_from_fd
        with self._retained_file(batch, "candidate.tmp", plan.candidate_bytes, complete_prefix=True) as descriptor:
            if plan.current_bytes is None:
                _publish_from_fd(descriptor, self.local, self.paths.profile.name)
            else:
                staged = self.store._read(batch, "candidate.publish", optional=True, limit=_MAX_TOTAL)
                if staged is None:
                    _publish_from_fd(descriptor, batch, "candidate.publish")
                    os.fsync(batch)
                elif staged != plan.candidate_bytes:
                    raise HostProfileStoreError(_UNSAFE)
                if self.store._read(self.local, self.paths.profile.name) != plan.current_bytes:
                    raise HostProfileStoreError(_UNSAFE)
                os.replace("candidate.publish", self.paths.profile.name,
                           src_dir_fd=batch, dst_dir_fd=self.local)
                os.fsync(batch)
            os.fsync(self.local)
            if self.store._read(self.local, self.paths.profile.name) != plan.candidate_bytes:
                raise HostProfileStoreError(_UNSAFE)

    def apply(self, plan):
        if self.inspect(plan) != plan.residue_state:
            raise HostProfileStoreError(_UNSAFE)
        name = self._batch_name(profile_digest(plan.marker_bytes), dict(plan.file_identities)[self.paths.transaction.name])
        content = plan.residue_state[1] if plan.residue_state else self._prepare(plan)
        if content is None:
            content = self._prepare(plan)
        if len(content) > _MAX_MANIFEST:
            raise HostProfileStoreError(_UNSAFE)
        manifest = self._parse(content, name)
        with self._directory(self.local, _ROOT, create=True) as root:
            names, total = self._inventory(root, name, plan)
            if name not in names and len(names) >= _MAX_BATCHES:
                raise HostProfileStoreError(_UNSAFE)
            # Reserve all remaining content plus both retained manifest copies
            # before moving the first candidate; there is no automatic pruning.
            remaining = sum(record["identity"][4] for record in manifest["files"]
                            if self._stat(self._source(record["name"]), record["name"]) is not None)
            with self._directory(root, name, create=True) as batch:
                def growth(filename, size):
                    metadata = self._stat(batch, filename)
                    return size if metadata is None else max(0, size - metadata.st_size)
                overhead = sum(growth(filename, len(content)) for filename in ("plan.tmp", "plan.json"))
                if plan.action == "publish-candidate":
                    planned = ("candidate.tmp", "candidate.publish") if plan.current_bytes is not None else ("candidate.tmp",)
                    overhead += sum(growth(filename, len(plan.candidate_bytes)) for filename in planned)
                if total + remaining + overhead > _MAX_RETAINED_BYTES:
                    raise HostProfileStoreError(_UNSAFE)
                self._publish_manifest(batch, content)
                self._pending(batch, manifest, plan)
                for record in manifest["files"][:-1]:
                    target = self._destination(record["name"])
                    if self._stat(batch, target) is None:
                        self._match(self._source(record["name"]), record["name"], record, moved=False)
                        _move_no_clobber(self._source(record["name"]), record["name"], batch, target)
                        os.fsync(batch)
                        os.fsync(self._source(record["name"]))
                        self._match(batch, target, record, moved=True)
                if plan.action == "publish-candidate":
                    self._publish_profile(batch, plan)
                self.store._verify_anchor(self.local, self.history)
                # Check the exact, now-complete archival prefix and canonical
                # source state before moving the marker as the commit point.
                current = self.store._inspect_state(self.local, self.history)
                self._pending(batch, manifest, current)
                marker = manifest["files"][-1]
                _move_no_clobber(self.local, marker["name"], batch, "marker.json")
                os.fsync(batch)
                os.fsync(self.local)
                self._match(batch, "marker.json", marker, moved=True)
                if self._stat(self.local, marker["name"]) is not None:
                    raise HostProfileStoreError(_UNSAFE)
                self._inventory(root, None)
                if self.store._inspect_state(self.local, self.history) is not None:
                    raise HostProfileStoreError(_UNSAFE)
