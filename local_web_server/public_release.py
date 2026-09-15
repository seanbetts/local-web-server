"""Fail-closed verification for the tracked public repository surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


POLICY_SCHEMA = "local-web-publication-policy/v1"
MAX_POLICY_BYTES = 64 * 1024
MAX_POLICY_VALUES = 256
MAX_POLICY_VALUE_BYTES = 4096
MAX_TEXT_FILE_BYTES = 512 * 1024
MAX_APPROVED_ASSET_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_TRACKED_FILES = 10_000
MAX_TRACKED_LIST_BYTES = 4 * 1024 * 1024
MAX_HISTORY_COMMITS = 10_000
MAX_DENIED_COMMIT_IDS = 10_000
MAX_DENIED_COMMIT_FILE_BYTES = 1024 * 1024

_GIT_TIMEOUT_SECONDS = 20.0
_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_GIT_CANDIDATES = (
    Path("/usr/bin/git"),
    Path("/opt/homebrew/bin/git"),
    Path("/usr/local/bin/git"),
)
_GIT_PREFIX = (
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "fsck.skipList=/dev/null",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.git.allow=never",
    "-c",
    "protocol.http.allow=never",
    "-c",
    "protocol.https.allow=never",
    "-c",
    "protocol.ssh.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "core.sshCommand=/usr/bin/false",
)

_REQUIRED_PUBLIC_FILES = (
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
_EXACT_PUBLIC_FILES = {
    "config/local/.gitignore": b"*\n!.gitignore\n!README.md\n",
}
_PUBLIC_DOCUMENT_SNIPPETS = {
    "README.md": (
        b"## Architecture",
        b"## Screenshots",
        b"## Prerequisites",
        b"## Bootstrap a host",
        b"## Create an application",
        b"## Security model",
        b"## Verification",
        b"## Case studies",
        b"## Limitations",
        b"snapshotData",
        b"viewState",
        b"apps/system-index/tests/snapshots/index-light-desktop.png",
        b"python3 scripts/install_local_web.py --dry-run",
        b"\npython3 scripts/install_local_web.py\n```",
    ),
    "SECURITY.md": (
        b"GitHub private vulnerability reporting",
        b"Do not post secrets",
    ),
    "CONTRIBUTING.md": (
        b"python3 -m unittest discover -s tests -v",
        b"npm run verify:host-profile",
        b"npm run verify:public-release",
        b"npm audit --omit=dev",
    ),
    "config/local/README.md": (
        b"apps.json",
        b"history/",
        b".host-profile-transaction.json",
        b"backups/",
        b"publication-policy.json",
        b".bundle",
        b".sha256",
        b"machine backups",
    ),
    "docs/architecture.md": (
        b"immutable releases",
        b"HostProfileStore",
        b"trusted-lan",
        b"tailscale-serve",
        b"127.0.0.1:8080",
    ),
    "docs/operations/host-profile.md": (
        b"local-web host init",
        b"local-web host migrate-registry",
        b"local-web host status",
        b"local-web host backup",
        b"local-web host restore",
        b"local-web host recover",
        b".host-profile-recovery",
        b"128 batches",
        b"256 MiB",
        b"no automatic pruning",
        b"excluded from revisions and backups",
        b"fails closed",
    ),
    "docs/operations/recovery.md": (
        b"local-web host status",
        b"local-web host recover",
        b"local-web host restore",
        b"local-web rollback",
        b"scripts/install_local_web.py --dry-run",
        b"\npython3 scripts/install_local_web.py\n```",
    ),
}
_PRIVATE_PLANNING_LINK = re.compile(
    r"\]\([^\n)]*(?:\.superpowers/|docs/superpowers/)"
)
_STALE_REGISTRY_GIT_WORDING = re.compile(
    r"\b(?:"
    r"(?:platform[- ]?)?registry\s+(?:git\s+)?commit"
    r"|committed\s+(?:the\s+)?(?:platform[- ]?)?registry"
    r"|commit(?:s|ted|ting)?\s+(?:a\s+|the\s+)?(?:platform[- ]?)?registry"
    r")\b",
    re.IGNORECASE,
)
_ALLOWED_ROOT_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "eslint.config.js",
        "package-lock.json",
        "package.json",
    }
)
_ALLOWED_ROOT_DIRECTORIES = frozenset(
    {
        ".github",
        "apps",
        "bin",
        "config",
        "docs",
        "examples",
        "local_web_server",
        "packages",
        "platform_assets",
        "scripts",
        "skills",
        "templates",
        "tests",
    }
)
_ALLOWED_CONFIG_PATHS = frozenset(
    {
        "config/apps.example.json",
        "config/local/.gitignore",
        "config/local/README.md",
    }
)
_ALLOWED_GITHUB_PATHS = frozenset({".github/workflows/ci.yml"})
_APPROVED_ASSET_DIGESTS = {
    "apps/system-index/tests/snapshots/index-dark-desktop.png": (
        "cdbfeb04706256d9fc9069d4f8d3370ea11660cfe542b875cae05152c7a39eae"
    ),
    "apps/system-index/tests/snapshots/index-dark-mobile.png": (
        "8216cf41a7bafa6124bcf33cb465d193dfece9f574d7fcd7947f7f23b6a06a44"
    ),
    "apps/system-index/tests/snapshots/index-light-desktop.png": (
        "af9a599537f3e95de38cdc27f65acd57ebc3190013be94292da55ebd5637b589"
    ),
    "apps/system-index/tests/snapshots/index-light-mobile.png": (
        "62fffc36769501b3cc92b974ce0b63c3118de067d1e3dc60805340124bfb93af"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-dark-compact.png": (
        "1aa9e87aaa312497ab98aa78c4f1c4abef091500dd21791867c8c6ba1486345f"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-dark-desktop.png": (
        "5b2da8f8b65b91df1982b61df41b4c9b7c7433ca47f4cf551a15d52a1538538e"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-dark-mobile.png": (
        "514254b9c4ae3801273e3f8439ef6cb121b1a7a3fa3bc56de00690c850db10b7"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-light-compact.png": (
        "7ae3a8f8ab20fbbd700dab56239fbc1883a9701d9f244981dc3456f22c907ee5"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-light-desktop.png": (
        "a43b671e0c78ca40dd8c0d01bff382d661e3f1b77eb91c025af877b0e625f40f"
    ),
    "examples/ui-gallery/tests/snapshots/gallery-light-mobile.png": (
        "0b7a066378ed18f444dfb8986f7c2819333d3a8aea7c87362f70fe7d441a6bf6"
    ),
}
_APPROVED_ASSET_PATHS = frozenset(_APPROVED_ASSET_DIGESTS)
_PRIVATE_TREE_PREFIXES = (
    ".superpowers/",
    "docs/superpowers/",
    "config/local/",
)
_GENERATED_COMPONENTS = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".vite",
        "__pycache__",
        "artifacts",
        "build",
        "coverage",
        "dist",
        "logs",
        "node_modules",
        "playwright-report",
        "releases",
        "runtime",
        "test-results",
        "tmp",
        "venv",
    }
)
_GENERATED_SUFFIXES = (
    ".coverage",
    ".log",
    ".pyc",
    ".pyo",
    ".tmp",
    ".tsbuildinfo",
)
_CREDENTIAL_EXACT_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
)
_CREDENTIAL_SUFFIXES = (
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
)
_CREDENTIAL_NAME_MARKERS = (
    "client-secret",
    "client_secret",
    "credential",
    "private-key",
    "private_key",
)

_COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_RULE = re.compile(r"[a-z][a-z0-9.-]{0,79}\Z")
_PERSONAL_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home)/([A-Za-z0-9._-]+)(?:/|\b)"
)
_PERSONAL_WINDOWS_PATH = re.compile(
    r"(?i)(?:[A-Z]:)?\\Users\\([A-Za-z0-9._-]+)(?:\\|\b)"
)
_TAILNET_HOST = re.compile(
    r"(?i)\b[a-z0-9](?:[a-z0-9.-]{0,249}[a-z0-9])?\.ts\.net\b"
)
_MAC_HOST = re.compile(
    r"\b[A-Za-z0-9][A-Za-z0-9-]{0,62}-(?:Mac-mini|Mac-Studio|Mac-Pro|MacBook-Pro|MacBook-Air)\b"
)
_VOLUME_DATABASE = re.compile(
    r"/Volumes/[A-Za-z0-9._ /-]+\.(?:db|sqlite|sqlite3)(?:\b|\Z)", re.IGNORECASE
)
_DATABASE_ASSIGNMENT = re.compile(r"(?im)^\s*DATABASE_URL\s*=")
_CREDENTIAL_URL = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:/@]+:[^\s/@]+@"
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{40,255})\b"
)
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b")
_STRIPE_SECRET = re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,255}\b")
_ABSOLUTE_ENVIRONMENT_FILE = re.compile(
    r"(?<![A-Za-z0-9])(/[A-Za-z0-9._~+ -]+(?:/[A-Za-z0-9._~+ -]+)*"
    r"/(?:\.env(?:\.[A-Za-z0-9._-]+)?|[A-Za-z0-9._-]+\.env))"
)
_PEM_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED " + b"PRIVATE KEY-----",
    b"-----BEGIN RSA " + b"PRIVATE KEY-----",
    b"-----BEGIN EC " + b"PRIVATE KEY-----",
    b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class _GitInspectionError(RuntimeError):
    pass


class _SecureReadError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrivatePublicationPolicy:
    deny_strings: tuple[str, ...] = field(repr=False)

    @property
    def deny_bytes(self) -> tuple[bytes, ...]:
        return tuple(value.encode("utf-8") for value in self.deny_strings)


@dataclass(frozen=True, slots=True)
class _TrackedEntry:
    relative: str
    mode: str
    object_id: str


@dataclass(frozen=True, slots=True)
class _RepositoryContext:
    root: Path
    lexical_root: Path


@dataclass(frozen=True, slots=True)
class _CommitSnapshot:
    repository: _RepositoryContext
    commit_id: str
    entries: tuple[_TrackedEntry, ...]


class PublicReleaseError(ValueError):
    """One sanitised public-release rule failure."""

    def __init__(
        self,
        rule: str,
        relative_path: str | None = None,
        *,
        private_values: Iterable[str] = (),
    ):
        safe_rule = rule if _SAFE_RULE.fullmatch(rule) else "verification.failure"
        self.rule = safe_rule
        self.relative_path = _safe_display_path(relative_path, private_values)
        message = f"public-release rule {safe_rule} failed"
        if self.relative_path is not None:
            message += f" for {self.relative_path}"
        super().__init__(message[:320])


def _safe_display_path(
    relative_path: str | None, private_values: Iterable[str]
) -> str | None:
    if relative_path is None:
        return None
    if any(value and value in relative_path for value in private_values):
        return "<redacted-path>"
    escaped = json.dumps(relative_path, ensure_ascii=True)[1:-1]
    if len(escaped) > 240:
        return "<overlong-path>"
    return escaped


def _safe_git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
        and name not in {"SSH_ASKPASS", "SSH_AUTH_SOCK"}
    }
    environment.update(
        {
            "PATH": _SAFE_PATH,
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "SSH_ASKPASS": "/usr/bin/false",
        }
    )
    return environment


def _git_executable() -> str:
    for candidate in _GIT_CANDIDATES:
        try:
            metadata = candidate.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(candidate, os.X_OK):
            return os.fspath(candidate)
    raise _GitInspectionError


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, 9)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_git(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    maximum_stdout: int,
    input_bytes: bytes | None = None,
    deadline: float | None = None,
    timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    if maximum_stdout < 0:
        raise _GitInspectionError
    operation_deadline = (
        time.monotonic() + timeout_seconds if deadline is None else deadline
    )
    if operation_deadline <= time.monotonic():
        raise _GitInspectionError
    input_file = None
    try:
        if input_bytes is not None:
            input_file = tempfile.TemporaryFile()
            input_file.write(input_bytes)
            input_file.seek(0)
        process = subprocess.Popen(
            (
                _git_executable(),
                *_GIT_PREFIX,
                "-C",
                os.fspath(repository),
                *arguments,
            ),
            env=_safe_git_environment(),
            stdin=subprocess.DEVNULL if input_file is None else input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        if input_file is not None:
            input_file.close()
        raise _GitInspectionError from error
    if process.stdout is None:
        _stop_process(process)
        if input_file is not None:
            input_file.close()
        raise _GitInspectionError

    output = bytearray()
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        reached_eof = False
        while not reached_eof:
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                raise _GitInspectionError
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                events = selector.select(0)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), min(65_536, maximum_stdout + 1 - len(output)))
                if not chunk:
                    reached_eof = True
                    selector.unregister(key.fileobj)
                    break
                output.extend(chunk)
                if len(output) > maximum_stdout:
                    raise _GitInspectionError
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            raise _GitInspectionError
        returncode = process.wait(timeout=remaining)
        return returncode, bytes(output)
    except (OSError, subprocess.SubprocessError, _GitInspectionError) as error:
        _stop_process(process)
        if isinstance(error, _GitInspectionError):
            raise
        raise _GitInspectionError from error
    finally:
        selector.close()
        process.stdout.close()
        if input_file is not None:
            input_file.close()


def _repository_context(
    repository: Path, *, deadline: float | None = None
) -> _RepositoryContext:
    try:
        lexical_root = Path(os.path.abspath(os.fspath(repository)))
        root = lexical_root.resolve(strict=True)
        metadata = root.stat()
    except (OSError, TypeError, ValueError) as error:
        raise PublicReleaseError("repository.invalid") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicReleaseError("repository.invalid")
    try:
        returncode, output = _run_git(
            root,
            ("rev-parse", "--show-toplevel"),
            maximum_stdout=4096,
            deadline=deadline,
        )
        top = Path(output.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError, _GitInspectionError) as error:
        raise PublicReleaseError("repository.invalid") from error
    if returncode != 0 or top != root:
        raise PublicReleaseError("repository.invalid")
    return _RepositoryContext(root=root, lexical_root=lexical_root)


def _canonical_git_path(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise PublicReleaseError("path.encoding") from error
    pure = PurePosixPath(value)
    if (
        not value
        or len(encoded) > 1024
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(
            not part
            or part in {".", "..", ".git"}
            or len(part.encode("utf-8")) > 255
            or "\\" in part
            or any(character in part for character in "*?[")
            or unicodedata.normalize("NFC", part) != part
            or any(unicodedata.category(character).startswith("C") for character in part)
            for part in pure.parts
        )
    ):
        raise PublicReleaseError("path.encoding")
    return value


def _head_commit_id(repository: Path, *, deadline: float) -> str:
    try:
        returncode, output = _run_git(
            repository,
            ("rev-parse", "--verify", "HEAD^{commit}"),
            maximum_stdout=128,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    try:
        commit_id = output.decode("ascii").strip()
    except UnicodeError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode != 0 or not _COMMIT_ID.fullmatch(commit_id):
        raise PublicReleaseError("repository.git")
    return commit_id


def _assert_index_matches(
    repository: Path, commit_id: str, *, deadline: float
) -> None:
    try:
        returncode, _ = _run_git(
            repository,
            ("diff-index", "--cached", "--quiet", commit_id, "--"),
            maximum_stdout=0,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode == 1:
        raise PublicReleaseError("tree.staged")
    if returncode != 0:
        raise PublicReleaseError("repository.git")


def _tree_entries(
    repository: Path, commit_id: str, *, deadline: float
) -> tuple[_TrackedEntry, ...]:
    try:
        returncode, output = _run_git(
            repository,
            ("ls-tree", "-r", "-z", "--full-tree", commit_id),
            maximum_stdout=MAX_TRACKED_LIST_BYTES,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode != 0:
        raise PublicReleaseError("repository.git")
    records = [record for record in output.split(b"\0") if record]
    if len(records) > MAX_TRACKED_FILES:
        raise PublicReleaseError("tree.bounds")
    entries: list[_TrackedEntry] = []
    seen: dict[str, str] = {}
    canonical_seen: set[str] = set()
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, raw_type, raw_object_id = metadata.split(b" ")
            relative = _canonical_git_path(raw_path.decode("utf-8"))
            mode_value = mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii")
        except (ValueError, UnicodeError, PublicReleaseError) as error:
            raise PublicReleaseError("path.encoding") from error
        if (
            mode_value not in {"100644", "100755", "120000", "160000"}
            or object_type != ("commit" if mode_value == "160000" else "blob")
            or not _COMMIT_ID.fullmatch(object_id)
            or relative in seen
        ):
            raise PublicReleaseError("tree.object")
        canonical_key = unicodedata.normalize("NFC", relative).casefold()
        if canonical_key in canonical_seen:
            raise PublicReleaseError("path.collision")
        canonical_seen.add(canonical_key)
        seen[relative] = mode_value
        entries.append(_TrackedEntry(relative, mode_value, object_id))
    return tuple(sorted(entries, key=lambda entry: entry.relative.encode("utf-8")))


def _index_paths(repository: Path, *, deadline: float) -> frozenset[str]:
    try:
        returncode, output = _run_git(
            repository,
            ("ls-files", "-z", "--cached"),
            maximum_stdout=MAX_TRACKED_LIST_BYTES,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode != 0:
        raise PublicReleaseError("repository.git")
    records = [record for record in output.split(b"\0") if record]
    if len(records) > MAX_TRACKED_FILES:
        raise PublicReleaseError("tree.bounds")
    try:
        return frozenset(_canonical_git_path(record.decode("utf-8")) for record in records)
    except (UnicodeError, PublicReleaseError) as error:
        raise PublicReleaseError("path.encoding") from error


def _commit_snapshot(
    context: _RepositoryContext, *, deadline: float
) -> _CommitSnapshot:
    commit_id = _head_commit_id(context.root, deadline=deadline)
    _assert_index_matches(context.root, commit_id, deadline=deadline)
    return _CommitSnapshot(
        repository=context,
        commit_id=commit_id,
        entries=_tree_entries(context.root, commit_id, deadline=deadline),
    )


def _assert_snapshot_unchanged(snapshot: _CommitSnapshot, *, deadline: float) -> None:
    if _head_commit_id(snapshot.repository.root, deadline=deadline) != snapshot.commit_id:
        raise PublicReleaseError("repository.changed")
    _assert_index_matches(snapshot.repository.root, snapshot.commit_id, deadline=deadline)


def _batch_object_sizes(
    repository: Path, object_ids: tuple[str, ...], *, deadline: float
) -> dict[str, int]:
    if not object_ids:
        return {}
    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
    try:
        returncode, output = _run_git(
            repository,
            ("cat-file", "--batch-check"),
            maximum_stdout=len(object_ids) * 128,
            input_bytes=request,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode != 0:
        raise PublicReleaseError("repository.git")
    lines = output.splitlines()
    if len(lines) != len(object_ids):
        raise PublicReleaseError("repository.git")
    sizes: dict[str, int] = {}
    for requested, line in zip(object_ids, lines, strict=True):
        try:
            actual, object_type, raw_size = line.decode("ascii").split(" ")
        except (UnicodeError, ValueError) as error:
            raise PublicReleaseError("repository.git") from error
        if (
            actual != requested
            or object_type != "blob"
            or not raw_size.isdigit()
            or requested in sizes
        ):
            raise PublicReleaseError("repository.git")
        sizes[requested] = int(raw_size)
    return sizes


def _batch_object_contents(
    repository: Path,
    object_ids: tuple[str, ...],
    sizes: dict[str, int],
    *,
    deadline: float,
) -> dict[str, bytes]:
    if not object_ids:
        return {}
    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
    maximum = sum(sizes.values()) + len(object_ids) * 128
    try:
        returncode, output = _run_git(
            repository,
            ("cat-file", "--batch"),
            maximum_stdout=maximum,
            input_bytes=request,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("repository.git") from error
    if returncode != 0:
        raise PublicReleaseError("repository.git")

    contents: dict[str, bytes] = {}
    position = 0
    for requested in object_ids:
        header_end = output.find(b"\n", position)
        if header_end < 0:
            raise PublicReleaseError("repository.git")
        try:
            actual, object_type, raw_size = output[position:header_end].decode(
                "ascii"
            ).split(" ")
        except (UnicodeError, ValueError) as error:
            raise PublicReleaseError("repository.git") from error
        size = sizes.get(requested)
        if (
            actual != requested
            or object_type != "blob"
            or size is None
            or raw_size != str(size)
        ):
            raise PublicReleaseError("repository.git")
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(output) or output[content_end : content_end + 1] != b"\n":
            raise PublicReleaseError("repository.git")
        contents[requested] = output[content_start:content_end]
        position = content_end + 1
    if position != len(output):
        raise PublicReleaseError("repository.git")
    return contents


def is_safe_worktree_mode(mode: int) -> bool:
    """Return whether a tracked blob's working-tree node is a regular file."""

    return stat.S_ISREG(mode)


def _validate_worktree_type(repository: Path, relative: str) -> None:
    current = repository
    parts = PurePosixPath(relative).parts
    try:
        for component in parts[:-1]:
            current = current / component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise PublicReleaseError("path.file-type", relative)
        path = repository.joinpath(*parts)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not is_safe_worktree_mode(metadata.st_mode):
            raise PublicReleaseError("path.file-type", relative)
    except PublicReleaseError:
        raise
    except OSError as error:
        raise PublicReleaseError("path.file-type", relative) from error


def _is_credential_path(relative: str) -> bool:
    name = PurePosixPath(relative).name.casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    return (
        name in _CREDENTIAL_EXACT_NAMES
        or name.endswith(_CREDENTIAL_SUFFIXES)
        or any(marker in name for marker in _CREDENTIAL_NAME_MARKERS)
    )


def _is_generated_path(relative: str) -> bool:
    pure = PurePosixPath(relative)
    folded_parts = {part.casefold() for part in pure.parts}
    name = pure.name.casefold()
    return bool(folded_parts & _GENERATED_COMPONENTS) or name.endswith(
        _GENERATED_SUFFIXES
    )


def _path_rule(relative: str) -> str | None:
    if relative == "config/apps.json":
        return "tree.private-path"
    if any(relative.startswith(prefix) for prefix in _PRIVATE_TREE_PREFIXES):
        if relative not in {
            "config/local/.gitignore",
            "config/local/README.md",
        }:
            return "tree.private-path"
    if _is_credential_path(relative):
        return "path.credential"
    if _is_generated_path(relative):
        return "path.generated"
    pure = PurePosixPath(relative)
    if len(pure.parts) == 1:
        if relative not in _ALLOWED_ROOT_FILES:
            return "path.allowlist"
    elif pure.parts[0] not in _ALLOWED_ROOT_DIRECTORIES:
        return "path.allowlist"
    if relative.startswith("config/") and relative not in _ALLOWED_CONFIG_PATHS:
        return "path.allowlist"
    if relative.startswith(".github/") and relative not in _ALLOWED_GITHUB_PATHS:
        return "path.allowlist"
    return None


def _contains_private_policy_value(
    relative: str, content: bytes | None, policy: PrivatePublicationPolicy | None
) -> bool:
    if policy is None:
        return False
    encoded_path = relative.encode("utf-8")
    return any(
        value in encoded_path or (content is not None and value in content)
        for value in policy.deny_bytes
    )


def _raw_credential_rule(content: bytes) -> str | None:
    if any(marker in content for marker in _PEM_MARKERS):
        return "content.credential"
    searchable = content.decode("latin-1")
    if any(
        pattern.search(searchable)
        for pattern in (
            _AWS_ACCESS_KEY,
            _GITHUB_TOKEN,
            _SLACK_TOKEN,
            _STRIPE_SECRET,
            _CREDENTIAL_URL,
        )
    ):
        return "content.credential"
    return None


def _is_documented_environment_placeholder(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"/Users/example/Coding/[a-z][a-z0-9-]*/\.env", value
    ) is not None


def _has_unsafe_environment_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            folded = re.sub(r"[-_]", "", key).casefold() if isinstance(key, str) else ""
            if folded == "environmentfile" and not _is_documented_environment_placeholder(
                child
            ):
                return True
            if _has_unsafe_environment_field(child):
                return True
    elif isinstance(value, list):
        return any(_has_unsafe_environment_field(child) for child in value)
    return False


def _config_example_rule(value: object) -> str | None:
    if not isinstance(value, dict):
        return "tree.example-registry"
    schema_version = value.get("schemaVersion")
    if schema_version == 1:
        expected_keys = {"schemaVersion", "host", "runtimeRoot", "apps"}
        if value.get("host") != "example-mac.local":
            return "tree.example-registry"
    elif schema_version == 2:
        expected_keys = {
            "schemaVersion",
            "publicOrigin",
            "ingressMode",
            "runtimeRoot",
            "apps",
        }
        if (
            value.get("publicOrigin") != "https://local.example.ts.net"
            or value.get("ingressMode") != "tailscale-serve"
        ):
            return "tree.example-registry"
    else:
        return "tree.example-registry"
    if set(value) != expected_keys or value.get("runtimeRoot") not in {
        "/Users/example/Library/Application Support/LocalWebServer",
        "/Users/example/Coding/runtime",
    }:
        return "tree.example-registry"
    apps = value.get("apps")
    if not isinstance(apps, list) or not apps:
        return "tree.example-registry"
    allowed_app_keys = {
        "id",
        "repository",
        "autoDeploy",
        "environmentFile",
        "environment",
        "port",
        "startCommand",
    }
    seen: set[str] = set()
    for app in apps:
        if (
            not isinstance(app, dict)
            or not {"id", "repository", "autoDeploy"}.issubset(app)
            or not set(app).issubset(allowed_app_keys)
        ):
            return "tree.example-registry"
        identifier = app.get("id")
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"example-[a-z0-9-]+", identifier) is None
            or identifier in seen
            or app.get("repository") != f"/Users/example/Coding/{identifier}"
            or type(app.get("autoDeploy")) is not bool
        ):
            return "tree.example-registry"
        seen.add(identifier)
        if "environmentFile" in app and app["environmentFile"] != (
            f"/Users/example/Coding/{identifier}/.env"
        ):
            return "content.environment-file"
        environment = app.get("environment")
        if environment is not None and (
            not isinstance(environment, dict)
            or any(
                not isinstance(name, str)
                or not name
                or re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None
                or any(
                    marker in name
                    for marker in ("PASSWORD", "TOKEN", "SECRET", "KEY", "CREDENTIAL")
                )
                or not isinstance(setting, str)
                for name, setting in environment.items()
            )
        ):
            return "tree.example-registry"
        port = app.get("port")
        command = app.get("startCommand")
        if (port is None) != (command is None):
            return "tree.example-registry"
        if port is not None and (
            type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            return "tree.example-registry"
    return None


def _index_fixture_is_generic(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "apps"}:
        return False
    apps = value.get("apps")
    if value.get("schemaVersion") != 1 or not isinstance(apps, list) or not apps:
        return False
    expected_keys = {
        "id",
        "title",
        "route",
        "icon",
        "accent",
        "frontendHealthPath",
        "backendHealthPath",
    }
    seen: set[str] = set()
    for app in apps:
        if not isinstance(app, dict) or set(app) != expected_keys:
            return False
        identifier = app.get("id")
        route = f"/{identifier}/" if isinstance(identifier, str) else ""
        backend = app.get("backendHealthPath")
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"example-[a-z0-9-]+", identifier) is None
            or identifier in seen
            or not isinstance(app.get("title"), str)
            or not app["title"].startswith("Example ")
            or app.get("route") != route
            or not isinstance(app.get("icon"), str)
            or re.fullmatch(r"[a-z][a-z0-9-]*", app["icon"]) is None
            or not isinstance(app.get("accent"), str)
            or re.fullmatch(r"#[0-9A-F]{6}", app["accent"]) is None
            or app.get("frontendHealthPath") != route
            or (
                backend is not None
                and (
                    not isinstance(backend, str)
                    or not backend.startswith("/")
                    or identifier not in backend
                )
            )
        ):
            return False
        seen.add(identifier)
    return True


def _text_content_rule(relative: str, text: str) -> str | None:
    if any(
        unicodedata.category(character).startswith("C")
        and character not in "\n\r\t"
        for character in text
    ):
        return "content.binary"
    for match in _ABSOLUTE_ENVIRONMENT_FILE.finditer(text):
        if not _is_documented_environment_placeholder(match.group(1)):
            return "content.environment-file"
    for match in _PERSONAL_POSIX_PATH.finditer(text):
        if match.group(1).casefold() != "example":
            return "content.personal-path"
    for match in _PERSONAL_WINDOWS_PATH.finditer(text):
        if match.group(1).casefold() != "example":
            return "content.personal-path"
    if _VOLUME_DATABASE.search(text) or _DATABASE_ASSIGNMENT.search(text):
        return "content.database"
    if _MAC_HOST.search(text):
        return "content.host"
    for match in _TAILNET_HOST.finditer(text):
        if match.group(0).casefold() != "local.example.ts.net":
            return "content.tailnet"
    if relative.endswith(".json"):
        try:
            decoded = json.loads(text, object_pairs_hook=_duplicate_object_pairs)
        except (TypeError, ValueError, _DuplicateJsonKey):
            decoded = None
        if relative == "config/apps.example.json":
            return _config_example_rule(decoded)
        if relative == "apps/system-index/fixtures/registry-v1.json":
            return None if _index_fixture_is_generic(decoded) else "content.inventory"
        if _has_unsafe_environment_field(decoded):
            return "content.environment-file"
        if (
            isinstance(decoded, dict)
            and isinstance(decoded.get("apps"), list)
            and ("schemaVersion" in decoded or "runtimeRoot" in decoded)
        ):
            return "content.inventory"
    return None


def _duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _absolute_components(path: Path) -> tuple[str, ...]:
    parts = path.parts
    return tuple(parts[1:] if parts and parts[0] == path.anchor else parts)


def _lexical_policy_path(
    repository: _RepositoryContext, policy_path: Path
) -> tuple[Path, str]:
    try:
        raw = os.fspath(policy_path)
        if not raw or "\0" in raw or "\\" in raw:
            raise OSError
        raw_parts = raw.split("/")
        is_absolute = raw.startswith("/")
        if is_absolute:
            raw_parts = raw_parts[1:]
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise OSError
        parts = tuple(raw_parts)
        if is_absolute:
            prefixes = (
                _absolute_components(repository.lexical_root),
                _absolute_components(repository.root),
            )
            relative_parts: tuple[str, ...] | None = None
            for prefix in prefixes:
                if len(parts) > len(prefix) and parts[: len(prefix)] == prefix:
                    relative_parts = parts[len(prefix) :]
                    break
            if relative_parts is None:
                raise OSError
        else:
            relative_parts = parts
        relative = _canonical_git_path(PurePosixPath(*relative_parts).as_posix())
        absolute = repository.root.joinpath(*relative_parts)
    except (OSError, TypeError, ValueError, PublicReleaseError) as error:
        raise PublicReleaseError("policy.path") from error
    return absolute, relative


def _read_beneath_repository(
    repository: Path,
    relative: str,
    *,
    maximum: int,
) -> bytes:
    descriptors: list[int] = []
    try:
        descriptor = os.open(repository, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise _SecureReadError("path")
        parent_metadata = os.fstat(descriptors[-1])
        if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise _SecureReadError("permissions")
        file_descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=descriptors[-1])
        descriptors.append(file_descriptor)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _SecureReadError("path")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _SecureReadError("permissions")
        if metadata.st_size > maximum:
            raise _SecureReadError("bounds")
        chunks: list[bytes] = []
        count = 0
        while True:
            chunk = os.read(file_descriptor, min(65_536, maximum + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                raise _SecureReadError("bounds")
            chunks.append(chunk)
        final = os.fstat(file_descriptor)
        if (
            final.st_dev != metadata.st_dev
            or final.st_ino != metadata.st_ino
            or final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or final.st_ctime_ns != metadata.st_ctime_ns
            or stat.S_IMODE(final.st_mode) != 0o600
            or final.st_nlink != 1
        ):
            raise _SecureReadError("path")
        return b"".join(chunks)
    except _SecureReadError:
        raise
    except OSError as error:
        raise _SecureReadError("path") from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_beneath_repository_path(repository: Path, relative: str) -> None:
    descriptors: list[int] = []
    try:
        descriptor = os.open(repository, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(descriptor)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise _SecureReadError("path")
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=descriptors[-1])
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _SecureReadError("path")
    except _SecureReadError:
        raise
    except OSError as error:
        raise _SecureReadError("path") from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_private_policy(
    context: _RepositoryContext,
    policy_path: Path,
    *,
    deadline: float,
    tracked: set[str],
) -> PrivatePublicationPolicy:
    _, relative = _lexical_policy_path(context, policy_path)
    if relative in tracked:
        raise PublicReleaseError("policy.tracked")
    try:
        _validate_beneath_repository_path(context.root, relative)
    except _SecureReadError as error:
        raise PublicReleaseError("policy.path") from error
    try:
        returncode, _ = _run_git(
            context.root,
            ("check-ignore", "--quiet", "--no-index", "--", relative),
            maximum_stdout=0,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("policy.ignored") from error
    if returncode != 0:
        raise PublicReleaseError("policy.ignored")
    try:
        content = _read_beneath_repository(
            context.root, relative, maximum=MAX_POLICY_BYTES
        )
    except _SecureReadError as error:
        rule = {
            "bounds": "policy.bounds",
            "permissions": "policy.permissions",
        }.get(error.category, "policy.path")
        raise PublicReleaseError(rule) from error
    try:
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=_duplicate_object_pairs
        )
    except (UnicodeError, ValueError, _DuplicateJsonKey) as error:
        raise PublicReleaseError("policy.schema") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "denyStrings"}
        or payload.get("schema") != POLICY_SCHEMA
    ):
        raise PublicReleaseError("policy.schema")
    values = payload.get("denyStrings")
    if not isinstance(values, list):
        raise PublicReleaseError("policy.values")
    if not values:
        raise PublicReleaseError("policy.values")
    if len(values) > MAX_POLICY_VALUES:
        raise PublicReleaseError("policy.bounds")
    checked: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise PublicReleaseError("policy.values")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError as error:
            raise PublicReleaseError("policy.values") from error
        if len(encoded) > MAX_POLICY_VALUE_BYTES:
            raise PublicReleaseError("policy.bounds")
        canonical = unicodedata.normalize("NFC", value)
        if (
            not value.strip()
            or canonical != value
            or any(unicodedata.category(character).startswith("C") for character in value)
            or canonical in seen
        ):
            raise PublicReleaseError("policy.values")
        seen.add(canonical)
        checked.append(value)
    return PrivatePublicationPolicy(tuple(checked))


def load_private_policy(
    repository: Path, policy_path: Path
) -> PrivatePublicationPolicy:
    """Load one ignored, mode-0600 private deny-string policy."""

    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    context = _repository_context(repository, deadline=deadline)
    commit_id = _head_commit_id(context.root, deadline=deadline)
    tracked = {
        entry.relative
        for entry in _tree_entries(context.root, commit_id, deadline=deadline)
    } | set(_index_paths(context.root, deadline=deadline))
    return _load_private_policy(
        context,
        policy_path,
        deadline=deadline,
        tracked=tracked,
    )


def _verify_public_tree_snapshot(
    snapshot: _CommitSnapshot,
    *,
    private_policy_path: Path | None,
    deadline: float,
) -> None:
    root = snapshot.repository.root
    entries = snapshot.entries
    tracked = {entry.relative for entry in entries}
    policy = (
        None
        if private_policy_path is None
        else _load_private_policy(
            snapshot.repository,
            private_policy_path,
            deadline=deadline,
            tracked=tracked | set(_index_paths(root, deadline=deadline)),
        )
    )
    private_values = () if policy is None else policy.deny_strings
    for required in _REQUIRED_PUBLIC_FILES:
        if required not in tracked:
            raise PublicReleaseError("tree.required", required)

    for entry in entries:
        relative = entry.relative
        if _contains_private_policy_value(relative, None, policy):
            raise PublicReleaseError(
                "content.private-policy",
                relative,
                private_values=private_values,
            )
        rule = _path_rule(relative)
        if rule is not None:
            raise PublicReleaseError(rule, relative, private_values=private_values)
        if entry.mode not in {"100644", "100755"}:
            raise PublicReleaseError(
                "path.file-type", relative, private_values=private_values
            )
        _validate_worktree_type(root, relative)

    object_ids = tuple(sorted({entry.object_id for entry in entries}))
    sizes = _batch_object_sizes(root, object_ids, deadline=deadline)
    total_bytes = 0
    for entry in entries:
        size = sizes.get(entry.object_id)
        maximum = (
            MAX_APPROVED_ASSET_BYTES
            if entry.relative in _APPROVED_ASSET_PATHS
            else MAX_TEXT_FILE_BYTES
        )
        if size is None or size > maximum:
            raise PublicReleaseError(
                "content.size", entry.relative, private_values=private_values
            )
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise PublicReleaseError("content.total-size")
    contents = _batch_object_contents(root, object_ids, sizes, deadline=deadline)

    package_content: bytes | None = None
    license_content: bytes | None = None
    for entry in entries:
        relative = entry.relative
        content = contents[entry.object_id]
        if _contains_private_policy_value(relative, content, policy):
            raise PublicReleaseError(
                "content.private-policy",
                relative,
                private_values=private_values,
            )
        raw_rule = _raw_credential_rule(content)
        if raw_rule is not None:
            raise PublicReleaseError(
                raw_rule, relative, private_values=private_values
            )
        if relative in _APPROVED_ASSET_PATHS:
            if not content.startswith(_PNG_SIGNATURE):
                raise PublicReleaseError("content.asset", relative)
            if hashlib.sha256(content).hexdigest() != _APPROVED_ASSET_DIGESTS[relative]:
                raise PublicReleaseError("content.asset-digest", relative)
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeError as error:
            raise PublicReleaseError("content.binary", relative) from error
        content_rule = _text_content_rule(relative, text)
        if content_rule is not None:
            raise PublicReleaseError(
                content_rule, relative, private_values=private_values
            )
        exact = _EXACT_PUBLIC_FILES.get(relative)
        if exact is not None and content != exact:
            raise PublicReleaseError(
                "tree.private-ignore", relative, private_values=private_values
            )
        required_snippets = _PUBLIC_DOCUMENT_SNIPPETS.get(relative)
        if required_snippets is not None and (
            any(snippet not in content for snippet in required_snippets)
            or _PRIVATE_PLANNING_LINK.search(text) is not None
        ):
            raise PublicReleaseError(
                "tree.documentation", relative, private_values=private_values
            )
        if (
            relative == "README.md"
            or relative.startswith(("docs/", "skills/", "templates/"))
        ) and _STALE_REGISTRY_GIT_WORDING.search(text) is not None:
            raise PublicReleaseError(
                "tree.documentation", relative, private_values=private_values
            )
        if relative == "package.json":
            package_content = content
        elif relative == "LICENSE":
            license_content = content

    try:
        package = json.loads((package_content or b"").decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise PublicReleaseError("package.private", "package.json") from error
    if not isinstance(package, dict) or package.get("private") is not True:
        raise PublicReleaseError("package.private", "package.json")
    if license_content is None or any(
        snippet not in license_content
        for snippet in (
            b"MIT License",
            b"Permission is hereby granted, free of charge",
            b'THE SOFTWARE IS PROVIDED "AS IS"',
        )
    ):
        raise PublicReleaseError("tree.license", "LICENSE")
    _assert_snapshot_unchanged(snapshot, deadline=deadline)


def verify_public_tree(
    repository: Path,
    *,
    private_policy_path: Path | None = None,
) -> None:
    """Verify the exact committed blobs and tracked-path surface."""

    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    context = _repository_context(repository, deadline=deadline)
    snapshot = _commit_snapshot(context, deadline=deadline)
    _verify_public_tree_snapshot(
        snapshot,
        private_policy_path=private_policy_path,
        deadline=deadline,
    )


def _read_external_secure_file(path: Path, maximum: int) -> bytes:
    descriptor: int | None = None
    try:
        before = Path(path).lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum
        ):
            raise _SecureReadError("path")
        descriptor = os.open(path, _FILE_FLAGS)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > maximum
        ):
            raise _SecureReadError("path")
        chunks: list[bytes] = []
        count = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                raise _SecureReadError("path")
            chunks.append(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_dev != opened.st_dev
            or final.st_ino != opened.st_ino
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or final.st_nlink != 1
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise _SecureReadError("path")
        return b"".join(chunks)
    except _SecureReadError:
        raise
    except OSError as error:
        raise _SecureReadError("path") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _denied_commit_ids(
    repository: Path, path: Path, tracked: set[str], *, deadline: float
) -> frozenset[str]:
    requested = Path(path)
    try:
        lexical = Path(os.path.abspath(requested))
        absolute = lexical.parent.resolve(strict=True) / lexical.name
    except (OSError, TypeError) as error:
        raise PublicReleaseError("history.deny-file") from error
    try:
        relative = absolute.relative_to(repository)
    except ValueError:
        relative = None
    if relative is not None:
        try:
            canonical = _canonical_git_path(PurePosixPath(*relative.parts).as_posix())
        except PublicReleaseError as error:
            raise PublicReleaseError("history.deny-file") from error
        if canonical in tracked:
            raise PublicReleaseError("history.deny-file")
        try:
            returncode, _ = _run_git(
                repository,
                ("check-ignore", "--quiet", "--no-index", "--", canonical),
                maximum_stdout=0,
                deadline=deadline,
            )
        except _GitInspectionError as error:
            raise PublicReleaseError("history.deny-file") from error
        if returncode != 0:
            raise PublicReleaseError("history.deny-file")
    try:
        content = _read_external_secure_file(
            absolute, MAX_DENIED_COMMIT_FILE_BYTES
        )
        text = content.decode("ascii")
    except (UnicodeError, _SecureReadError) as error:
        raise PublicReleaseError("history.deny-file") from error
    lines = text.splitlines()
    if (
        not lines
        or len(lines) > MAX_DENIED_COMMIT_IDS
        or any(not _COMMIT_ID.fullmatch(line) for line in lines)
        or len(set(lines)) != len(lines)
    ):
        raise PublicReleaseError("history.deny-file")
    return frozenset(lines)


def _history_output(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    maximum: int,
    rule: str,
    deadline: float,
) -> bytes:
    try:
        returncode, output = _run_git(
            repository,
            arguments,
            maximum_stdout=maximum,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError(rule) from error
    if returncode != 0:
        raise PublicReleaseError(rule)
    return output


def _validate_branch_name(branch: str) -> str:
    if (
        not isinstance(branch, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch)
        or branch.startswith("-")
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise PublicReleaseError("history.branch")
    return f"refs/heads/{branch}"


def _public_refs(
    snapshot: _CommitSnapshot,
    *,
    expected_branch: str,
    deadline: float,
) -> None:
    expected_ref = _validate_branch_name(expected_branch)
    refs = _history_output(
        snapshot.repository.root,
        ("for-each-ref", "--format=%(refname)%00%(objectname)%00%(objecttype)"),
        maximum=1024 * 1024,
        rule="history.refs",
        deadline=deadline,
    )
    parsed: dict[str, tuple[str, str]] = {}
    try:
        records = [line for line in refs.decode("utf-8").splitlines() if line]
        for record in records:
            name, object_id, object_type = record.split("\0")
            if (
                name in parsed
                or not name.startswith("refs/")
                or not _COMMIT_ID.fullmatch(object_id)
                or object_type != "commit"
            ):
                raise ValueError
            parsed[name] = (object_id, object_type)
    except (UnicodeError, ValueError) as error:
        raise PublicReleaseError("history.refs") from error
    if expected_ref not in parsed:
        raise PublicReleaseError("history.branch")
    if parsed[expected_ref][0] != snapshot.commit_id:
        raise PublicReleaseError("history.commit-binding")

    # A normal single-branch transport clone may add only these two aliases.
    # Both must resolve to the exact checked commit; origin/HEAD must be symbolic.
    remote_ref = f"refs/remotes/origin/{expected_branch}"
    remote_head = "refs/remotes/origin/HEAD"
    allowed = {expected_ref}
    if remote_ref in parsed:
        allowed.add(remote_ref)
        if parsed[remote_ref][0] != snapshot.commit_id:
            raise PublicReleaseError("history.refs")
    if remote_head in parsed:
        allowed.add(remote_head)
        if remote_ref not in parsed or parsed[remote_head][0] != snapshot.commit_id:
            raise PublicReleaseError("history.refs")
        try:
            returncode, output = _run_git(
                snapshot.repository.root,
                ("symbolic-ref", "--quiet", remote_head),
                maximum_stdout=256,
                deadline=deadline,
            )
        except _GitInspectionError as error:
            raise PublicReleaseError("history.refs") from error
        try:
            symbolic_target = output.decode("utf-8").strip()
        except UnicodeError as error:
            raise PublicReleaseError("history.refs") from error
        if returncode != 0 or symbolic_target != remote_ref:
            raise PublicReleaseError("history.refs")
    if set(parsed) != allowed:
        raise PublicReleaseError("history.refs")


def _verify_public_history_snapshot(
    snapshot: _CommitSnapshot,
    *,
    expected_branch: str,
    denied_commit_ids_path: Path,
    deadline: float,
) -> None:
    root = snapshot.repository.root
    tracked = {entry.relative for entry in snapshot.entries} | set(
        _index_paths(root, deadline=deadline)
    )
    denied = _denied_commit_ids(
        root, denied_commit_ids_path, tracked, deadline=deadline
    )
    shallow = _history_output(
        root,
        ("rev-parse", "--is-shallow-repository"),
        maximum=16,
        rule="history.shallow",
        deadline=deadline,
    )
    if shallow != b"false\n":
        raise PublicReleaseError("history.shallow")
    _public_refs(snapshot, expected_branch=expected_branch, deadline=deadline)

    try:
        returncode, _ = _run_git(
            root,
            ("fsck", "--full", "--strict", "--no-reflogs"),
            maximum_stdout=0,
            deadline=deadline,
        )
    except _GitInspectionError as error:
        raise PublicReleaseError("history.fsck") from error
    if returncode != 0:
        raise PublicReleaseError("history.fsck")

    roots = _history_output(
        root,
        ("rev-list", "--max-parents=0", "--max-count=3", snapshot.commit_id),
        maximum=256,
        rule="history.root-count",
        deadline=deadline,
    ).splitlines()
    if len(roots) != 1 or not _COMMIT_ID.fullmatch(roots[0].decode("ascii", "ignore")):
        raise PublicReleaseError("history.root-count")
    merges = _history_output(
        root,
        ("rev-list", "--min-parents=2", "--max-count=1", snapshot.commit_id),
        maximum=128,
        rule="history.merge",
        deadline=deadline,
    )
    if merges.strip():
        raise PublicReleaseError("history.merge")
    commits = _history_output(
        root,
        (
            "rev-list",
            f"--max-count={MAX_HISTORY_COMMITS + 1}",
            snapshot.commit_id,
        ),
        maximum=(MAX_HISTORY_COMMITS + 1) * 65,
        rule="history.bounds",
        deadline=deadline,
    ).splitlines()
    if len(commits) > MAX_HISTORY_COMMITS:
        raise PublicReleaseError("history.bounds")
    try:
        reachable = {value.decode("ascii") for value in commits}
    except UnicodeError as error:
        raise PublicReleaseError("history.bounds") from error
    if any(not _COMMIT_ID.fullmatch(value) for value in reachable):
        raise PublicReleaseError("history.bounds")
    if reachable & denied:
        raise PublicReleaseError("history.pre-public")
    _public_refs(snapshot, expected_branch=expected_branch, deadline=deadline)
    _assert_snapshot_unchanged(snapshot, deadline=deadline)


def verify_public_history(
    repository: Path,
    *,
    expected_branch: str,
    denied_commit_ids_path: Path,
) -> None:
    """Verify one public branch without merge or pre-public reachability."""

    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    context = _repository_context(repository, deadline=deadline)
    snapshot = _commit_snapshot(context, deadline=deadline)
    _verify_public_history_snapshot(
        snapshot,
        expected_branch=expected_branch,
        denied_commit_ids_path=denied_commit_ids_path,
        deadline=deadline,
    )


def verify_public_release(
    repository: Path,
    *,
    private_policy_path: Path | None = None,
    expected_branch: str | None = None,
    denied_commit_ids_path: Path | None = None,
) -> None:
    """Run the tracked-tree gate and, when requested, the history gate."""

    if (expected_branch is None) != (denied_commit_ids_path is None):
        raise PublicReleaseError("arguments.history")
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    context = _repository_context(repository, deadline=deadline)
    snapshot = _commit_snapshot(context, deadline=deadline)
    _verify_public_tree_snapshot(
        snapshot,
        private_policy_path=private_policy_path,
        deadline=deadline,
    )
    if expected_branch is not None and denied_commit_ids_path is not None:
        _verify_public_history_snapshot(
            snapshot,
            expected_branch=expected_branch,
            denied_commit_ids_path=denied_commit_ids_path,
            deadline=deadline,
        )
