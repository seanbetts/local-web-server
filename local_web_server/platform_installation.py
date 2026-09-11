"""Reusable orchestration for building and installing the local-web platform."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent_skill import (
    AgentSkillInstallError,
    AgentSkillInstallResult,
    AgentSkillInstaller,
)
from .config import ConfigError, load_registry, load_registry_bytes
from .git_runner import GitRunnerError, run_git
from .host_profile import HostProfileError, HostProfilePaths, resolve_host_profile
from .install import InstallError, InstallResult, Installer, load_main_manifests
from .process_runner import ProcessCommand, ProcessRunError, ProcessRunner


@dataclass(frozen=True)
class PlatformInstallationResult:
    install: InstallResult
    agent_skill: AgentSkillInstallResult


_INSTALLATION_SOURCES = (
    "package.json",
    "package-lock.json",
    "apps/system-index",
    "examples/ui-gallery",
    "packages/ui",
    "platform_assets",
    "skills/local-web-app-development",
    "local_web_server",
    "scripts",
)
_INVALID_PRIVATE_PROFILE = (
    "private host profile is invalid; run local-web host restore"
)


def require_committed_installation_sources(
    repository: Path, *, require_main: bool = False
) -> None:
    """Refuse to publish platform code or assets that are not in Git."""

    try:
        platform = Path(repository).resolve(strict=True)
        if not platform.is_dir():
            raise OSError
        result = run_git(
            platform,
            (
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *_INSTALLATION_SOURCES,
            ),
            maximum_stdout=1024 * 1024,
        )
        if result.returncode != 0:
            raise OSError
        if result.stdout:
            raise InstallError(
                "platform installation sources have uncommitted changes"
            )
        if require_main:
            branch = run_git(
                platform,
                (
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                ),
                maximum_stdout=256,
            )
            if branch.returncode != 0 or branch.stdout.strip() != "main":
                raise OSError
    except InstallError:
        raise
    except (GitRunnerError, OSError, RuntimeError, ValueError) as error:
        raise InstallError("platform installation sources cannot be verified") from error


def install_platform(
    repository: Path,
    *,
    dry_run: bool,
    recover_missing_caddy: bool = False,
    prepare_tailscale_port_migration: bool = False,
    registry_path: Path | None = None,
) -> PlatformInstallationResult:
    """Build reviewed UI surfaces and install their host-owned artifacts."""
    platform = Path(repository)
    try:
        require_committed_installation_sources(
            platform, require_main=not dry_run
        )
        if registry_path is None:
            selected_registry = resolve_host_profile(platform)
            try:
                from .host_profile_backup import (
                    MAX_BACKUP_BYTES,
                    MAX_PROFILE_BYTES,
                    MAX_REVISION_BYTES,
                    MAX_REVISIONS,
                )
                from .host_profile_store import HostProfileStore, HostProfileStoreError

                paths = HostProfilePaths.for_repository(platform)
                if paths.profile != selected_registry:
                    raise HostProfileError(_INVALID_PRIVATE_PROFILE)
                snapshot = HostProfileStore(paths).read_snapshot(
                    max_profile_bytes=MAX_PROFILE_BYTES,
                    max_revision_bytes=MAX_REVISION_BYTES,
                    max_revisions=MAX_REVISIONS,
                    max_total_bytes=MAX_BACKUP_BYTES,
                )
                registry = load_registry_bytes(snapshot.profile_bytes)
            except (ConfigError, HostProfileStoreError, UnicodeError) as error:
                raise HostProfileError(_INVALID_PRIVATE_PROFILE) from error
        else:
            selected_registry = Path(registry_path)
            registry = load_registry(selected_registry)
        ProcessRunner().run(
            platform,
            (
                ProcessCommand("system-index-build", ("npm", "run", "build:index")),
                ProcessCommand("ui-gallery-build", ("npm", "run", "build:gallery")),
            ),
        )
        manifests = load_main_manifests(registry)
        migration_options = (
            {"prepare_tailscale_port_migration": True} if prepare_tailscale_port_migration else {}
        )
        installed = Installer(
            registry,
            manifests,
            repository=platform,
        ).install(
            dry_run,
            recover_missing_caddy=recover_missing_caddy,
            **migration_options,
        )
        skill = AgentSkillInstaller(repository=platform).install(dry_run)
        return PlatformInstallationResult(installed, skill)
    except (ConfigError, InstallError):
        raise
    except ProcessRunError as error:
        raise InstallError("system-index-build failed") from error
    except AgentSkillInstallError as error:
        raise InstallError("agent skill installation failed") from error
