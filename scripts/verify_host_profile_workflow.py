#!/usr/bin/env python3
"""Verify the private host-profile lifecycle in one disposable root."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server.config import load_registry
from local_web_server.host_commands import HostCommands
from local_web_server.host_profile import (
    HostProfilePaths,
    canonical_json_bytes,
)
from local_web_server.host_profile_backup import (
    MAX_BACKUP_BYTES,
    MAX_PROFILE_BYTES,
    MAX_REVISIONS,
    MAX_REVISION_BYTES,
    parse_host_backup,
)
from local_web_server.host_profile_store import HostProfileStore
from local_web_server.install import Installer, load_main_manifests
from local_web_server.render import render_caddyfile
from scripts.disposable_workflow_support import sanitized_subprocess_environment


PHASES = (
    "migrate legacy registry into private profile",
    "verify exact profile revision backup and modes",
    "verify redacted host status",
    "verify disposable install and Caddy configuration",
    "verify explicit private backup",
    "restore backup into absent private profile",
    "recover untouched transaction",
    "recover published revision",
    "recover published profile",
    "cleanup",
)

_APP_ID = "host-profile-runtime-fixture"
_MAX_CADDY_OUTPUT = 256 * 1024
_MAX_RESIDUE_BYTES = 256 * 1024 * 1024


class HostProfileWorkflowError(RuntimeError):
    """A bounded workflow failure that never includes private state."""


@dataclass(frozen=True, slots=True)
class WorkflowEvidence:
    phases: tuple[str, ...]
    profile_relative: Path
    restored_profile_relative: Path
    directory_modes: tuple[int, int, int]
    file_modes: tuple[int, int, int]
    revision_counts: tuple[int, int]
    recovery_actions: tuple[str, str, str]
    caddy_validated: bool
    all_paths_disposable: bool
    status_redacted: bool
    backup_round_tripped: bool
    recovery_residue_retained: bool
    owned_paths: tuple[Path, ...]


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(microseconds=1)
        return value


class _Interrupted(RuntimeError):
    pass


class _InterruptingStore(HostProfileStore):
    def __init__(self, paths: HostProfilePaths, stop: str, clock: Callable[[], datetime]):
        super().__init__(paths, clock=clock)
        self._stop = stop
        self._stopped = False

    def _atomic(self, directory, temporary, destination, content, **kwargs):
        result = super()._atomic(
            directory, temporary, destination, content, **kwargs
        )
        is_revision = destination[:1].isdigit() and destination.endswith(".json")
        should_stop = (
            (self._stop == "marker" and destination == self.paths.transaction.name)
            or (self._stop == "revision" and is_revision)
            or (self._stop == "profile" and destination == self.paths.profile.name)
        )
        if should_stop and not self._stopped:
            self._stopped = True
            raise _Interrupted
        return result


def select_caddy(
    *,
    homebrew: Path = Path("/opt/homebrew/bin/caddy"),
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Prefer the standard macOS Homebrew Caddy, then an executable PATH hit."""

    preferred = Path(homebrew)
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return preferred
    fallback = which("caddy")
    if fallback is not None:
        selected = Path(fallback)
        if selected.is_file() and os.access(selected, os.X_OK):
            return selected
    raise HostProfileWorkflowError("Caddy validation is unavailable")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("/usr/bin/git", "-C", str(repository), *arguments),
        env=sanitized_subprocess_environment(
            {},
            overrides={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or len(result.stdout) > 8192:
        raise HostProfileWorkflowError("disposable Git operation failed")
    return result.stdout.strip()


def _commit_fixture(repository: Path) -> None:
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "user.name=Host Profile Workflow",
        "-c",
        "user.email=host-profile@invalid",
        "commit",
        "-m",
        "fixture",
    )


def _create_repository(repository: Path) -> None:
    repository.mkdir()
    (repository / "config").mkdir()
    (repository / "local_web_server").mkdir()
    (repository / "local_web_server/source.py").write_text(
        "# disposable host profile fixture\n", encoding="utf-8"
    )
    (repository / ".gitignore").write_text(
        "config/local/\n", encoding="utf-8"
    )
    _git(repository, "init", "-q", "-b", "main")


def _create_app(repository: Path) -> None:
    repository.mkdir()
    (repository / "dist").mkdir()
    (repository / "dist/index.html").write_text(
        "<!doctype html><title>Host profile fixture</title>\n",
        encoding="utf-8",
    )
    (repository / "local-web.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": _APP_ID,
                "title": "Host Profile Runtime Fixture",
                "route": "/host-profile-runtime-fixture",
                "kind": "static",
                "build": {
                    "commands": [["/usr/bin/true"]],
                    "output": "dist",
                    "environment": [],
                },
                "healthPath": "/host-profile-runtime-fixture/",
                "home": {"icon": "book", "accent": "#8EA7C6"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _commit_fixture(repository)


def _create_bundles(platform: Path) -> None:
    theme = platform / "platform_assets/theme.css"
    theme.parent.mkdir(parents=True)
    shutil.copyfile(PLATFORM_REPOSITORY / "platform_assets/theme.css", theme)

    index = platform / "apps/system-index/dist"
    (index / "assets").mkdir(parents=True)
    (index / "index.html").write_text(
        "<!doctype html><script src='/assets/index.js'></script>\n",
        encoding="utf-8",
    )
    (index / "assets/index.js").write_text(
        "console.log('host profile fixture');\n", encoding="utf-8"
    )

    gallery = platform / "examples/ui-gallery/dist"
    (gallery / "assets").mkdir(parents=True)
    (gallery / "index.html").write_text(
        "<!doctype html><script src='/assets/gallery.js'></script>\n",
        encoding="utf-8",
    )
    (gallery / "assets/gallery.js").write_text(
        "console.log('host profile gallery');\n", encoding="utf-8"
    )


def _registry_bytes(root: Path, app: Path, *, host: str) -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "host": host,
                "runtimeRoot": str(root / "runtime"),
                "apps": [
                    {
                        "id": _APP_ID,
                        "repository": str(app),
                        "autoDeploy": False,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _variant(content: bytes, host: str) -> bytes:
    value = json.loads(content)
    value["host"] = host
    return canonical_json_bytes(value)


def _validate_caddy(caddy: Path, caddyfile: Path, root: Path) -> None:
    environment_root = root / "caddy-environment"
    home = environment_root / "home"
    temporary = environment_root / "tmp"
    config = environment_root / "config"
    data = environment_root / "data"
    for directory in (home, temporary, config, data):
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    result = subprocess.run(
        (
            str(caddy),
            "validate",
            "--config",
            str(caddyfile),
            "--adapter",
            "caddyfile",
        ),
        env=sanitized_subprocess_environment(
            {},
            overrides={
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(data),
            },
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if (
        result.returncode != 0
        or len(result.stdout) > _MAX_CADDY_OUTPUT
        or len(result.stderr) > _MAX_CADDY_OUTPUT
    ):
        raise HostProfileWorkflowError("disposable Caddy validation failed")


def _recover_state(
    repository: Path,
    before: bytes,
    after: bytes,
    *,
    stop: str,
    action: str,
    clock: Callable[[], datetime],
) -> tuple[str, bytes]:
    paths = HostProfilePaths.for_repository(repository)
    interrupted = _InterruptingStore(paths, stop, clock)
    try:
        interrupted.publish_registration(_APP_ID, before, after)
    except _Interrupted:
        pass
    else:
        raise HostProfileWorkflowError("recovery fixture did not interrupt")

    commands = HostCommands(repository, clock=clock)
    plan = commands.preview_recovery()
    if plan.action != action:
        raise HostProfileWorkflowError("recovery action was invalid")
    result = commands.apply(plan)
    expected = before if action == "remove-untouched-marker" else after
    store = HostProfileStore(paths)
    if (
        not result.applied
        or paths.transaction.exists()
        or store.read_current() != expected
        or store.inspect_recovery() is not None
    ):
        raise HostProfileWorkflowError("recovery result was invalid")
    return action, expected


def run_workflow(root: Path, *, caddy: Path) -> WorkflowEvidence:
    """Run every host-profile acceptance step beneath one caller-owned root."""

    try:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise OSError
        caddy = Path(caddy).resolve(strict=True)
        if not caddy.is_file() or not os.access(caddy, os.X_OK):
            raise OSError

        app = root / "app"
        platform = root / "platform"
        restored = root / "restored"
        home = root / "home"
        rendered_root = root / "rendered"
        exported = root / "exports"
        for directory in (home, rendered_root, exported):
            directory.mkdir(mode=0o700)
        _create_app(app)
        _create_repository(platform)
        _create_bundles(platform)
        legacy = platform / "config/apps.json"
        legacy_content = _registry_bytes(
            root, app.resolve(), host="host-profile.invalid"
        )
        legacy.write_bytes(legacy_content)
        _commit_fixture(platform)

        clock = _Clock()
        commands = HostCommands(platform, clock=clock)
        preview = commands.preview_migration()
        if (
            preview.operation != "migrate-registry"
            or preview.action != "initialise"
            or HostProfilePaths.for_repository(platform).local.exists()
        ):
            raise HostProfileWorkflowError("migration preview was invalid")
        migrated = commands.apply(preview)
        paths = HostProfilePaths.for_repository(platform)
        store = HostProfileStore(paths)
        revisions = store.revisions()
        migrated_backup = parse_host_backup(migrated.backup_path)
        if (
            not migrated.applied
            or migrated.revision_id != revisions[0].revision_id
            or legacy.read_bytes() != legacy_content
            or paths.profile.read_bytes() != legacy_content
            or migrated_backup.profile_bytes != legacy_content
            or tuple(item.content for item in migrated_backup.revisions)
            != tuple(item.envelope_bytes for item in revisions)
        ):
            raise HostProfileWorkflowError("migrated profile was invalid")

        directory_modes = tuple(
            stat.S_IMODE(directory.stat().st_mode)
            for directory in (paths.local, paths.history, paths.backups)
        )
        initial_revision_path = next(paths.history.iterdir())

        status = commands.status().as_dict()
        status_text = json.dumps(status, sort_keys=True, separators=(",", ":"))
        redacted_text = status_text.replace(str(paths.profile), "<profile>")
        status_redacted = (
            status["transactionState"] == "clean"
            and status["revisionCount"] == 1
            and status["lastBackupAt"] is not None
            and all(
                marker not in redacted_text
                for marker in (
                    _APP_ID,
                    "host-profile.invalid",
                    str(app),
                    str(root / "runtime"),
                )
            )
        )

        registry = load_registry(paths.profile)
        manifests = load_main_manifests(registry)
        installer = Installer(
            registry,
            manifests,
            repository=platform,
            home=home,
        )
        install = installer.install(dry_run=True)
        if (
            not install.dry_run
            or not install.writes
            or any(not path.is_relative_to(root) for path in install.writes)
        ):
            raise HostProfileWorkflowError("disposable install preview was invalid")
        caddyfile = rendered_root / "Caddyfile"
        caddyfile.write_text(
            render_caddyfile(registry, manifests), encoding="utf-8"
        )
        caddyfile.chmod(0o600)
        _validate_caddy(caddy, caddyfile, root)

        explicit_backup = exported / "host-profile-backup.json"
        backed_up = commands.backup(explicit_backup)
        backup = parse_host_backup(explicit_backup)
        backup_round_tripped = (
            backed_up.applied
            and backup.profile_bytes == legacy_content
            and tuple(item.content for item in backup.revisions)
            == tuple(item.envelope_bytes for item in revisions)
        )

        _create_repository(restored)
        _commit_fixture(restored)
        restored_commands = HostCommands(restored, clock=clock)
        restore_preview = restored_commands.preview_restore(explicit_backup)
        restored_result = restored_commands.apply(restore_preview)
        restored_paths = HostProfilePaths.for_repository(restored)
        restored_store = HostProfileStore(restored_paths)
        restored_revisions = restored_store.revisions()
        if (
            not restored_result.applied
            or restored_store.read_current() != legacy_content
            or tuple(item.envelope_bytes for item in restored_revisions)
            != tuple(item.content for item in backup.revisions)
        ):
            raise HostProfileWorkflowError("restored profile was invalid")

        recovery_actions: list[str] = []
        current = legacy_content
        for stop, action, host in (
            ("marker", "remove-untouched-marker", "recovery-one.invalid"),
            ("revision", "publish-candidate", "recovery-two.invalid"),
            ("profile", "clear-marker", "recovery-three.invalid"),
        ):
            candidate = _variant(current, host)
            recovered_action, current = _recover_state(
                restored,
                current,
                candidate,
                stop=stop,
                action=action,
                clock=clock,
            )
            recovery_actions.append(recovered_action)

        residue = restored_paths.recovery_residue
        residue_files = tuple(
            path for path in residue.rglob("*") if path.is_file()
        )
        residue_batches = tuple(path for path in residue.iterdir() if path.is_dir())
        residue_bytes = sum(path.stat().st_size for path in residue_files)
        recovery_residue_retained = (
            stat.S_IMODE(residue.stat().st_mode) == 0o700
            # The exact restore is itself completed through recovery archival,
            # followed by the three deliberately interrupted publications.
            and len(residue_batches) == 4
            and 0 < residue_bytes <= _MAX_RESIDUE_BYTES
            and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in residue_files)
        )
        final_snapshot = HostProfileStore(restored_paths).read_snapshot(
            max_profile_bytes=MAX_PROFILE_BYTES,
            max_revision_bytes=MAX_REVISION_BYTES,
            max_revisions=MAX_REVISIONS,
            max_total_bytes=MAX_BACKUP_BYTES,
        )
        final_backup = HostCommands(restored, clock=clock).backup().backup_path
        final_document = parse_host_backup(final_backup)
        if (
            final_document.profile_bytes != final_snapshot.profile_bytes
            or tuple(item.content for item in final_document.revisions)
            != tuple(item.envelope_bytes for item in final_snapshot.revisions)
        ):
            raise HostProfileWorkflowError("recovery residue entered a backup")

        owned_paths = (
            app,
            platform,
            paths.profile,
            migrated.backup_path,
            root / "runtime",
            home,
            caddyfile,
            explicit_backup,
            restored,
            restored_paths.profile,
            residue,
            final_backup,
        )
        all_paths_disposable = all(path.is_relative_to(root) for path in owned_paths)
        file_modes = (
            stat.S_IMODE(paths.profile.stat().st_mode),
            stat.S_IMODE(initial_revision_path.stat().st_mode),
            stat.S_IMODE(explicit_backup.stat().st_mode),
        )
        return WorkflowEvidence(
            PHASES[:-1],
            paths.profile.relative_to(root),
            restored_paths.profile.relative_to(root),
            directory_modes,
            file_modes,
            (len(revisions), len(restored_revisions)),
            tuple(recovery_actions),
            True,
            all_paths_disposable,
            status_redacted,
            backup_round_tripped,
            recovery_residue_retained,
            owned_paths,
        )
    except HostProfileWorkflowError:
        raise
    except BaseException as error:
        raise HostProfileWorkflowError("host profile workflow failed") from error


class HostProfileWorkflowVerifier:
    def __init__(
        self,
        *,
        temporary_directory_factory: Callable[..., object] = tempfile.TemporaryDirectory,
        emit: Callable[[str], None] = print,
    ) -> None:
        self._temporary_directory_factory = temporary_directory_factory
        self._emit = emit

    def run(self) -> int:
        temporary = None
        succeeded = False
        cleanup_ok = False
        try:
            temporary = self._temporary_directory_factory(
                prefix="local-web-host-profile-workflow-"
            )
            evidence = run_workflow(
                Path(temporary.name), caddy=select_caddy()
            )
            if (
                evidence.phases != PHASES[:-1]
                or evidence.directory_modes != (0o700, 0o700, 0o700)
                or evidence.file_modes != (0o600, 0o600, 0o600)
                or evidence.revision_counts != (1, 1)
                or not all(
                    (
                        evidence.caddy_validated,
                        evidence.all_paths_disposable,
                        evidence.status_redacted,
                        evidence.backup_round_tripped,
                        evidence.recovery_residue_retained,
                    )
                )
            ):
                raise HostProfileWorkflowError("host profile evidence was invalid")
            for phase in PHASES[:-1]:
                self._emit(f"{phase} PASS")
            succeeded = True
        except BaseException:
            self._emit("host profile workflow FAIL")
        finally:
            if temporary is not None:
                try:
                    temporary.cleanup()
                    cleanup_ok = not Path(temporary.name).exists()
                except BaseException:
                    cleanup_ok = False
            self._emit(f"{PHASES[-1]} {'PASS' if cleanup_ok else 'FAIL'}")
        return 0 if succeeded and cleanup_ok else 1


def main() -> int:
    return HostProfileWorkflowVerifier().run()


if __name__ == "__main__":
    raise SystemExit(main())
