"""Safe, explicit installation of generated runtime files and managed hooks."""

import errno
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .config import ConfigError, probe_environment, validate_registered_app
from .git_build import GitRepository
from .index_registry import INDEX_REGISTRY_NAME, render_index_registry
from .ingress import IngressVerificationError, TailscaleServeIngressVerifier
from .models import AppManifest, HostRegistry
from .public_origin import TAILSCALE_CADDY_PORT, TAILSCALE_SERVE
from .render import (
    probe_environment_name,
    render_caddy_plist,
    render_caddyfile,
    render_hook,
    render_service_plist,
)
from .runtime import RuntimeLayout
from .system_index_bundle import load_system_index_bundle, load_ui_gallery_bundle
from .theme_gate import ThemeGate, ThemeReleaseError, ThemeReleaseGate


_CADDY = "/opt/homebrew/bin/caddy"
_CADDY_LABEL = "com.sean.local-web.caddy"
_MANAGED_HOOK_MARKER = "# managed-by-local-web-server"
_SLEEP_ADVICE = "System sleep is enabled on AC power. Run: sudo pmset -c sleep 0"
_AC_SECTION = re.compile(r"^AC Power:\s*$", re.MULTILINE)
_SLEEP_SETTING = re.compile(r"^\s*sleep\s+(\d+)\s*$", re.MULTILINE)
_CADDY_PID = re.compile(r"^\s*pid = ([1-9][0-9]*)\s*$", re.MULTILINE)
_CADDY_RUNNING = re.compile(r"^\s*state = running\s*$", re.MULTILINE)
_CADDY_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_CADDY_BOOTSTRAP_TIMEOUT_SECONDS = 2.0
_CADDY_RECOVERY_TIMEOUT_SECONDS = 10.0
_CADDY_RELOAD_TIMEOUT_SECONDS = 10.0
_CADDY_VALIDATION_TIMEOUT_SECONDS = 10.0
_CADDY_INSPECTION_TIMEOUT_SECONDS = 2.0
_CADDY_INGRESS_READINESS_TIMEOUT_SECONDS = 2.0
_CADDY_TRANSITION_POLL_SECONDS = 0.05


class InstallError(RuntimeError):
    """Installation could not complete without risking existing state."""


@dataclass(frozen=True)
class InstallResult:
    """The managed file targets and any operator action reported by installation."""

    writes: tuple[Path, ...]
    messages: tuple[str, ...]
    dry_run: bool
    dry_run_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CaddyPublication:
    """The effective live state and mutation performed by bundle publication."""

    live_loaded: bool
    activation: Literal["none", "reloaded", "replaced"]


@dataclass(frozen=True)
class _CaddyProcessIdentity:
    """One process instance captured from the exact managed launchd job."""

    pid: int
    started: str


class _CaddyTransitionError(InstallError):
    """A sanitised lifecycle phase failure with the process still in scope."""

    def __init__(
        self,
        message: str,
        process: _CaddyProcessIdentity | None = None,
        *,
        stage: Literal["pre-bootout", "bootout-failed", "post-bootout"],
    ):
        super().__init__(message)
        self.process = process
        self.stage = stage


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def load_main_manifests(
    registry: HostRegistry,
    *,
    repository_factory: Callable[[Path], object] = GitRepository,
) -> dict[str, AppManifest]:
    """Resolve and validate every manifest from its exact ``refs/heads/main`` commit."""
    manifests: dict[str, AppManifest] = {}
    try:
        for host in registry.apps:
            repository = repository_factory(host.repository)
            commit = repository.main_commit()
            _host, manifest = validate_registered_app(host, repository.manifest_at(commit))
            manifests[host.id] = manifest
    except ConfigError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise ConfigError("cannot resolve exact main manifests for installation") from error
    return manifests


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=timeout,
    )


def _private_directory(path: Path) -> None:
    """Create one installer-owned directory and keep it private to the user."""
    try:
        if path.is_symlink():
            raise InstallError(f"managed directory cannot be a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise InstallError(f"managed directory is not a directory: {path}")
        path.chmod(0o700)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError(f"cannot prepare managed directory: {path}") from error


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Replace *path* from a private temporary sibling."""
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise InstallError(f"cannot atomically write managed file: {path}") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Installer:
    """Install files atomically one-by-one; a safe rerun repairs partial installation."""

    def __init__(
        self,
        registry: HostRegistry,
        manifests: Mapping[str, AppManifest],
        *,
        repository: Path,
        home: Path | None = None,
        run: CommandRunner = _run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        theme_gate: ThemeGate | None = None,
        ingress_verifier: TailscaleServeIngressVerifier | None = None,
    ):
        self.registry = registry
        self.manifests = dict(manifests)
        self.repository = Path(repository).resolve()
        self.home = Path(home) if home is not None else Path.home()
        self.run = run
        self.monotonic = monotonic
        self.sleep = sleep
        self.theme_gate = theme_gate
        self.ingress_verifier = ingress_verifier or TailscaleServeIngressVerifier(run=run)
        self.launch_agents = self.home / "Library" / "LaunchAgents"
        self.caddyfile = Path(registry.runtime_root) / "Caddyfile"
        self.caddy_plist = self.launch_agents / f"{_CADDY_LABEL}.plist"
        self.theme_source = self.repository / "platform_assets" / "theme.css"

    def install(
        self,
        dry_run: bool = False,
        *,
        recover_missing_caddy: bool = False,
        prepare_tailscale_port_migration: bool = False,
    ) -> InstallResult:
        """Install files, activating only an already managed Caddy boundary.

        Dry-run conditional targets reflect only its current read-only snapshot.
        """
        try:
            from .theme import ThemeStore, validate_theme

            if prepare_tailscale_port_migration:
                if self.registry.ingress_mode != TAILSCALE_SERVE:
                    raise InstallError("port migration preparation requires Tailscale ingress")
                if recover_missing_caddy:
                    raise InstallError("port migration preparation cannot recover missing Caddy")
            theme_content = self._theme_content()
            accents = tuple(self._manifest(host.id).home.accent for host in self.registry.apps)
            validate_theme(theme_content, accents)
            theme_store = ThemeStore(self.registry.runtime_root)
            environment = self._probe_environment()
            rendered = self._rendered_files(
                environment, prepare_tailscale_port_migration=prepare_tailscale_port_migration,
            )
            self._preflight_hooks(rendered)
            current_home = Path(self.registry.runtime_root) / "home.html"
            previous_home = Path(self.registry.runtime_root) / "home.previous.html"
            messages = (*self._probe_messages(), *self._power_messages())
            if prepare_tailscale_port_migration:
                messages += (
                    "Port migration preparation is not final security activation; "
                    "wildcard port 80 remains until the separately approved final install.",
                )
            if dry_run:
                prior_home = self._managed_home_content(current_home)
                self._managed_home_content(previous_home)
                targets = self._installation_targets(
                    rendered,
                    theme_store.current,
                    current_home,
                    previous_home,
                    prior_home,
                )
                summary = (
                    (
                        "Ingress mode: Tailscale Serve (tailscale-serve).",
                        (
                            f"Preparation keeps wildcard port 80 and adds 127.0.0.1:{TAILSCALE_CADDY_PORT}."
                            if prepare_tailscale_port_migration else
                            f"Caddy will be loopback-only on 127.0.0.1:{TAILSCALE_CADDY_PORT}."
                        ),
                    )
                    if self.registry.ingress_mode == TAILSCALE_SERVE
                    else ()
                )
                return InstallResult(targets, messages, True, summary)

            if self._theme_changed(theme_store.current, theme_content):
                self._verify_theme_candidate(theme_content)

            tailscale_ingress = self.registry.ingress_mode == TAILSCALE_SERVE
            caddy_loaded = self._preflight_caddy_state(
                environment,
                recover_missing_caddy=recover_missing_caddy and not tailscale_ingress,
            )
            prior_caddy_process = None
            if tailscale_ingress:
                if caddy_loaded:
                    prior_caddy_process = self._loaded_caddy_process()
                if prior_caddy_process is None:
                    raise InstallError(
                        "Tailscale ingress requires an already loaded managed Caddy"
                    )
                try:
                    self.ingress_verifier.preflight(
                        self.registry, require_success=prepare_tailscale_port_migration,
                    )
                except IngressVerificationError:
                    raise InstallError("Tailscale HTTPS ingress preflight failed") from None
                self._preflight_tailscale_listener_transition(
                    environment, prepared=prepare_tailscale_port_migration,
                )
            prior_caddy = self._file_state(self.caddyfile)
            prior_plist = self._file_state(self.caddy_plist)
            self._prepare_directories()
            with theme_store.transaction():
                prior_theme = theme_store.snapshot()
                prior_home = self._managed_home_content(current_home)
                self._managed_home_content(previous_home)
                desired_home = rendered[current_home][0]
                retain_prior_home = (
                    prior_home is not None and prior_home != desired_home
                )
                targets = self._installation_targets(
                    rendered,
                    theme_store.current,
                    current_home,
                    previous_home,
                    prior_home,
                )
                caddy_published = False
                try:
                    caddy_publication = self._install_caddy_bundle(
                        rendered.pop(self.caddyfile),
                        rendered.pop(self.caddy_plist),
                        environment,
                        caddy_loaded=caddy_loaded,
                    )
                    caddy_published = True
                    if tailscale_ingress:
                        process = (
                            self._wait_for_caddy_process("Caddy candidate process did not start")
                            if caddy_publication.activation == "replaced"
                            else self._loaded_caddy_process()
                        )
                        if process is None:
                            raise InstallError("cannot identify loaded Caddy process")
                        try:
                            if caddy_publication.activation == "replaced":
                                self._wait_for_caddy_ingress(
                                    process, prepared=prepare_tailscale_port_migration,
                                )
                            else:
                                verify = (
                                    self.ingress_verifier.verify_prepared
                                    if prepare_tailscale_port_migration
                                    else self.ingress_verifier.verify_loaded
                                )
                                verify(self.registry, process.pid)
                        except IngressVerificationError:
                            raise InstallError("Tailscale HTTPS ingress verification failed") from None
                    theme_store.activate(theme_content)
                    for path, (content, mode) in rendered.items():
                        if path == current_home and retain_prior_home:
                            _atomic_write(previous_home, prior_home, 0o600)
                        _atomic_write(path, content, mode)
                except Exception as error:
                    if caddy_published:
                        try:
                            theme_store.restore(prior_theme)
                            self._restore_caddy_files(prior_caddy, prior_plist)
                            if caddy_publication.live_loaded:
                                if caddy_publication.activation == "reloaded":
                                    self._reload_loaded_caddy(environment)
                                    if (
                                        tailscale_ingress
                                        and self._loaded_caddy_process() != prior_caddy_process
                                    ):
                                        raise InstallError("Caddy recovery process identity changed")
                                elif caddy_publication.activation == "replaced":
                                    if caddy_loaded:
                                        self._restore_loaded_caddy_job()
                                    else:
                                        self._stop_loaded_caddy_job()
                        except (InstallError, ValueError, OSError) as recovery_error:
                            raise InstallError(
                                "installation failed; platform recovery failed"
                            ) from recovery_error
                    raise error
            return InstallResult(targets, messages, False)
        except InstallError:
            raise
        except (OSError, ValueError) as error:
            raise InstallError("cannot install generated local web files") from error

    @staticmethod
    def _theme_changed(current: Path, content: bytes) -> bool:
        descriptor = -1
        try:
            metadata = current.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise InstallError("managed theme file is unsafe")
            descriptor = os.open(
                current,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISREG(pinned.st_mode)
                or (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino)
            ):
                raise InstallError("managed theme file is unsafe")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                return source.read() != content
        except FileNotFoundError:
            return True
        except InstallError:
            raise
        except OSError as error:
            raise InstallError("cannot inspect managed theme file") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _verify_theme_candidate(self, content: bytes) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="local-web-install-theme-") as directory:
                candidate = Path(directory) / "candidate.css"
                candidate.write_bytes(content)
                candidate.chmod(0o600)
                gate = self.theme_gate or ThemeReleaseGate(self.repository)
                gate.verify(candidate)
        except ThemeReleaseError as error:
            raise InstallError("theme compatibility gate failed") from error
        except InstallError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise InstallError("theme compatibility gate failed") from error

    def _theme_content(self) -> bytes:
        """Read only the regular, repository-owned cosmetic theme file."""
        try:
            metadata = self.theme_source.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise InstallError("repository platform theme is not a regular file")
            return self.theme_source.read_bytes()
        except InstallError:
            raise
        except OSError as error:
            raise InstallError("cannot read repository platform theme") from error

    def _rendered_files(
        self, environment: tuple[tuple[str, str], ...] | None = None,
        *, prepare_tailscale_port_migration: bool = False,
    ) -> dict[Path, tuple[bytes, int]]:
        if environment is None:
            environment = self._probe_environment()
        try:
            index_bundle = load_system_index_bundle(
                self.repository / "apps" / "system-index" / "dist"
            )
            gallery_bundle = load_ui_gallery_bundle(
                self.repository / "examples" / "ui-gallery" / "dist"
            )
        except ValueError as error:
            raise InstallError("platform UI bundle is invalid") from error
        index_root = Path(self.registry.runtime_root) / "platform" / "index"
        files: dict[Path, tuple[bytes, int]] = {
            self.caddyfile: (
                render_caddyfile(
                    self.registry, self.manifests,
                    prepare_tailscale_port_migration=prepare_tailscale_port_migration,
                ).encode(),
                0o600,
            ),
            self.caddy_plist: (
                render_caddy_plist(
                    self.repository, self.registry.runtime_root, environment=environment
                ),
                0o600,
            ),
        }
        for relative_path, content in index_bundle.assets:
            files[index_root.joinpath(*relative_path.parts)] = (content, 0o600)
        gallery_root = Path(self.registry.runtime_root) / "platform" / "ui-gallery"
        for relative_path, content in gallery_bundle:
            files[gallery_root.joinpath(*relative_path.parts)] = (content, 0o600)
        files[index_root / INDEX_REGISTRY_NAME] = (
            render_index_registry(self.registry, self.manifests),
            0o600,
        )
        for host in self.registry.apps:
            manifest = self._manifest(host.id)
            if manifest.kind == "service":
                files[self.launch_agents / f"com.sean.local-web.{host.id}.plist"] = (
                    render_service_plist(
                        host, manifest, RuntimeLayout(self.registry.runtime_root, host.id)
                    ),
                    0o600,
                )
            if host.auto_deploy:
                hook = render_hook(host.id, self.repository).encode()
                for name in ("post-commit", "post-merge"):
                    files[host.repository / ".git" / "hooks" / name] = (hook, 0o700)
        files[Path(self.registry.runtime_root) / "home.html"] = (
            index_bundle.home,
            0o600,
        )
        return files

    @staticmethod
    def _installation_targets(
        rendered: Mapping[Path, tuple[bytes, int]],
        theme: Path,
        current_home: Path,
        previous_home: Path,
        prior_home: bytes | None,
    ) -> tuple[Path, ...]:
        targets = list(rendered)
        desired_home = rendered[current_home][0]
        if prior_home is not None and prior_home != desired_home:
            targets.insert(targets.index(current_home), previous_home)
        return (*targets, theme)

    @staticmethod
    def _managed_home_content(path: Path) -> bytes | None:
        descriptor = -1
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISREG(pinned.st_mode)
                or (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino)
            ):
                raise ValueError
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                return source.read()
        except FileNotFoundError:
            return None
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _probe_environment(self) -> tuple[tuple[str, str], ...]:
        """Resolve declared probe inputs into Caddy-private environment names."""
        environment: list[tuple[str, str]] = []
        names: set[str] = set()
        for host in self.registry.apps:
            manifest = self._manifest(host.id)
            for source_name, value in probe_environment(manifest, host).items():
                name = probe_environment_name(host.id, source_name)
                if name in names:
                    raise InstallError("duplicate generated Caddy probe environment name")
                names.add(name)
                environment.append((name, value))
        return tuple(sorted(environment))

    def _probe_messages(self) -> tuple[str, ...]:
        messages: list[str] = []
        for host in self.registry.apps:
            probe = host.backend_probe
            if probe is None:
                continue
            names = ", ".join(probe.public_environment)
            messages.append(f"{host.id} backend probe uses public environment: {names}")
        return tuple(messages)

    def _manifest(self, app_id: str) -> AppManifest:
        try:
            manifest = self.manifests[app_id]
        except KeyError as error:
            raise InstallError(f"missing manifest for registered app: {app_id}") from error
        if manifest.id != app_id:
            raise InstallError(f"manifest id does not match registered app: {app_id}")
        return manifest

    def _preflight_hooks(self, rendered: Mapping[Path, tuple[bytes, int]]) -> None:
        for path, (_, mode) in rendered.items():
            if mode != 0o700:
                continue
            if path.parent.is_symlink() or not path.parent.is_dir():
                raise InstallError(
                    f"repository hooks directory is not a real directory: {path.parent}"
                )
            if path.is_symlink():
                raise InstallError(f"refusing unmanaged Git hook: {path}")
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise InstallError(f"cannot inspect existing Git hook: {path}") from error
            if _MANAGED_HOOK_MARKER not in content.splitlines():
                raise InstallError(f"refusing unmanaged Git hook: {path}")

    def _prepare_directories(self) -> None:
        runtime_root = Path(self.registry.runtime_root)
        directories = [
            runtime_root,
            runtime_root / "apps",
            runtime_root / "platform" / "index",
            runtime_root / "platform" / "index" / "assets",
            runtime_root / "platform" / "ui-gallery",
            runtime_root / "platform" / "ui-gallery" / "assets",
            runtime_root / "logs",
            runtime_root / "logs" / "caddy",
            runtime_root / "logs" / "deploy",
            runtime_root / "logs" / "services",
            self.launch_agents,
        ]
        for host in self.registry.apps:
            layout = RuntimeLayout(runtime_root, host.id)
            directories.extend((layout.app_root, layout.releases))
        for directory in directories:
            _private_directory(directory)

    def _preflight_caddy_state(
        self,
        environment: tuple[tuple[str, str], ...],
        *,
        recover_missing_caddy: bool,
    ) -> bool:
        loaded = self._caddy_loaded()
        if loaded and (not self.caddyfile.is_file() or not self.caddy_plist.is_file()):
            raise InstallError(
                "Caddy is loaded but its managed Caddyfile or LaunchAgent plist is missing. "
                "Stop the loaded Caddy LaunchAgent before installing."
            )
        if loaded or not recover_missing_caddy:
            return loaded

        caddy_state = self._file_state(self.caddyfile)
        plist_state = self._file_state(self.caddy_plist)
        complete = caddy_state.content is not None and plist_state.content is not None
        if not complete:
            if caddy_state.content is not None or plist_state.content is not None:
                raise InstallError(
                    "Caddy recovery requires the complete managed Caddyfile and "
                    "LaunchAgent plist"
                )
            return False

        validation_deadline = (
            self.monotonic() + _CADDY_VALIDATION_TIMEOUT_SECONDS
        )
        validation = self._run_before_deadline(
            [
                _CADDY,
                "validate",
                "--config",
                str(self.caddyfile),
                "--adapter",
                "caddyfile",
            ],
            validation_deadline,
            message="Caddy interrupted-state validation timed out",
            env=self._caddy_environment(environment),
        )
        if validation.returncode != 0:
            raise InstallError("Caddy interrupted-state validation failed")
        if not self._bootstrap_caddy_job():
            raise InstallError("Caddy interrupted-state recovery failed")
        return True

    def _install_caddy_bundle(
        self,
        caddy_content: tuple[bytes, int],
        plist_content: tuple[bytes, int],
        environment: tuple[tuple[str, str], ...],
        *,
        caddy_loaded: bool,
    ) -> _CaddyPublication:
        """Install and activate Caddy config and private environment as one unit."""
        new_caddy, caddy_mode = caddy_content
        new_plist, plist_mode = plist_content
        caddy_was_loaded = caddy_loaded
        prior_caddy = self._file_state(self.caddyfile)
        prior_plist = self._file_state(self.caddy_plist)
        candidate = self._candidate(new_caddy, caddy_mode)
        try:
            validation_deadline = (
                self.monotonic() + _CADDY_VALIDATION_TIMEOUT_SECONDS
            )
            validation = self._run_before_deadline(
                [_CADDY, "validate", "--config", str(candidate), "--adapter", "caddyfile"],
                validation_deadline,
                message="Caddy candidate validation timed out",
                env=self._caddy_environment(environment),
            )
            if validation.returncode != 0:
                raise InstallError("Caddy candidate validation failed")
            live_caddy_loaded = self._caddy_loaded()
            if caddy_loaded and not live_caddy_loaded:
                try:
                    self._restore_loaded_caddy_job()
                except InstallError as recovery_error:
                    raise InstallError(
                        "Caddy LaunchAgent state changed during candidate validation; "
                        "LaunchAgent recovery failed"
                    ) from recovery_error
                raise InstallError(
                    "Caddy LaunchAgent state changed during candidate validation; "
                    "restored prior LaunchAgent"
                )
            if live_caddy_loaded and (
                prior_caddy.content is None or prior_plist.content is None
            ):
                raise InstallError(
                    "Caddy became loaded without a complete managed Caddyfile and "
                    "LaunchAgent plist. Stop the loaded Caddy LaunchAgent before installing."
                )
            caddy_loaded = live_caddy_loaded
            job_became_loaded = not caddy_was_loaded and caddy_loaded
            caddy_bytes_changed = prior_caddy.content != new_caddy
            plist_bytes_changed = prior_plist.content != new_plist
            listener_topology_changed = False
            if caddy_loaded and caddy_bytes_changed:
                topology_deadline = (
                    self.monotonic() + _CADDY_VALIDATION_TIMEOUT_SECONDS
                )
                prior_topology = self._caddy_listener_topology(
                    self.caddyfile, environment, topology_deadline
                )
                candidate_topology = self._caddy_listener_topology(
                    candidate, environment, topology_deadline
                )
                listener_topology_changed = prior_topology != candidate_topology
            replace_loaded_job = (
                plist_bytes_changed or job_became_loaded or listener_topology_changed
            )
            caddy_write = caddy_bytes_changed or prior_caddy.mode != caddy_mode
            plist_write = plist_bytes_changed or prior_plist.mode != plist_mode
            if not caddy_write and not plist_write:
                return _CaddyPublication(caddy_loaded, "none")

            try:
                if caddy_write:
                    _atomic_write(self.caddyfile, new_caddy, caddy_mode)
                if plist_write:
                    _atomic_write(self.caddy_plist, new_plist, plist_mode)
            except InstallError:
                self._restore_caddy_files(prior_caddy, prior_plist)
                raise

            if not caddy_loaded or not (caddy_bytes_changed or plist_bytes_changed):
                return _CaddyPublication(caddy_loaded, "none")

            try:
                if replace_loaded_job:
                    self._replace_loaded_caddy_job()
                    activation = "replaced"
                else:
                    self._reload_loaded_caddy(environment)
                    activation = "reloaded"
            except InstallError as activation_error:
                self._restore_caddy_files(prior_caddy, prior_plist)
                try:
                    if replace_loaded_job:
                        if (
                            isinstance(activation_error, _CaddyTransitionError)
                            and activation_error.stage == "pre-bootout"
                        ):
                            if not self._caddy_loaded():
                                raise InstallError(
                                    "Caddy pre-shutdown recovery state changed"
                                )
                        else:
                            process = (
                                activation_error.process
                                if isinstance(
                                    activation_error, _CaddyTransitionError
                                )
                                else None
                            )
                            preserve_loaded_process = (
                                isinstance(
                                    activation_error, _CaddyTransitionError
                                )
                                and activation_error.stage == "bootout-failed"
                            )
                            self._restore_loaded_caddy_job(
                                process,
                                preserve_loaded_process=preserve_loaded_process,
                            )
                    else:
                        self._reload_loaded_caddy(environment)
                except InstallError as recovery_error:
                    phase = (
                        "LaunchAgent"
                        if replace_loaded_job
                        else "configuration"
                    )
                    raise InstallError(
                        f"Caddy {phase} activation failed; restored prior files but recovery failed"
                    ) from recovery_error
                phase = (
                    "LaunchAgent"
                    if replace_loaded_job
                    else "configuration"
                )
                raise InstallError(
                    f"Caddy {phase} activation failed; restored prior configuration and running state"
                ) from activation_error
            return _CaddyPublication(True, activation)
        finally:
            candidate.unlink(missing_ok=True)

    @dataclass(frozen=True)
    class _FileState:
        content: bytes | None
        mode: int | None

    @staticmethod
    def _file_state(path: Path) -> _FileState:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise InstallError(f"managed Caddy file is not a regular file: {path}")
            return Installer._FileState(
                path.read_bytes(), stat.S_IMODE(metadata.st_mode)
            )
        except FileNotFoundError:
            return Installer._FileState(None, None)
        except InstallError:
            raise
        except OSError as error:
            raise InstallError(f"cannot inspect managed Caddy file: {path}") from error

    @staticmethod
    def _restore_file(path: Path, state: _FileState) -> None:
        if state.content is None:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                raise InstallError(f"cannot restore managed Caddy file: {path}") from error
            return
        if state.mode is None:
            raise InstallError(f"cannot restore managed Caddy file: {path}")
        _atomic_write(path, state.content, state.mode)

    def _restore_caddy_files(
        self, prior_caddy: _FileState, prior_plist: _FileState
    ) -> None:
        self._restore_file(self.caddyfile, prior_caddy)
        self._restore_file(self.caddy_plist, prior_plist)

    @staticmethod
    def _caddy_environment(
        environment: tuple[tuple[str, str], ...]
    ) -> dict[str, str]:
        process_environment = {
            name: value
            for name in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")
            if isinstance(value := os.environ.get(name), str)
        }
        process_environment.update(environment)
        return process_environment

    def _run_before_deadline(
        self,
        argv: Sequence[str],
        deadline: float,
        *,
        message: str,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise InstallError(message)
        try:
            return self.run(argv, env=env, timeout=remaining)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError(message) from error

    @staticmethod
    def _launch_domain() -> str:
        return f"gui/{os.getuid()}"

    @classmethod
    def _launch_service(cls) -> str:
        return f"{cls._launch_domain()}/{_CADDY_LABEL}"

    def _reload_loaded_caddy(
        self, environment: tuple[tuple[str, str], ...]
    ) -> None:
        deadline = self.monotonic() + _CADDY_RELOAD_TIMEOUT_SECONDS
        reload_result = self._run_before_deadline(
            [
                _CADDY,
                "reload",
                "--config",
                str(self.caddyfile),
                "--adapter",
                "caddyfile",
            ],
            deadline,
            message="Caddy configuration reload timed out",
            env=self._caddy_environment(environment),
        )
        if reload_result.returncode != 0:
            raise InstallError("Caddy configuration reload failed")

    def _preflight_tailscale_listener_transition(
        self, environment: tuple[tuple[str, str], ...], *, prepared: bool,
    ) -> None:
        """Reject unsafe handovers before publishing any managed artifacts."""
        prior = self._caddy_listener_topology(
            self.caddyfile, environment,
            self.monotonic() + _CADDY_INSPECTION_TIMEOUT_SECONDS,
        )
        final = (f"127.0.0.1:{TAILSCALE_CADDY_PORT}",)
        transitional = tuple(sorted((":80", *final)))
        allowed = ((":80",), transitional) if prepared else (transitional, final)
        if prior not in allowed:
            if not prepared and prior == (":80",):
                raise InstallError("Tailscale ingress requires port migration preparation")
            raise InstallError("unsupported Caddy listener topology for port migration")

    def _caddy_listener_topology(
        self,
        config: Path,
        environment: tuple[tuple[str, str], ...],
        deadline: float,
    ) -> tuple[str, ...]:
        message = "Caddy listener topology could not be determined"
        adapted = self._run_before_deadline(
            [_CADDY, "adapt", "--config", str(config), "--adapter", "caddyfile"],
            deadline,
            message=message,
            env=self._caddy_environment(environment),
        )
        if adapted.returncode != 0:
            raise InstallError(message)
        try:
            payload = json.loads(adapted.stdout)
            servers = payload["apps"]["http"]["servers"]
            if not isinstance(servers, dict) or not servers:
                raise ValueError
            listeners: list[str] = []
            for server in servers.values():
                if not isinstance(server, dict):
                    raise ValueError
                values = server.get("listen")
                if not isinstance(values, list) or not values:
                    raise ValueError
                if not all(isinstance(value, str) and value for value in values):
                    raise ValueError
                listeners.extend(values)
            return tuple(sorted(listeners))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise InstallError(message) from None

    def _replace_loaded_caddy_job(self) -> None:
        shutdown_deadline = (
            self.monotonic() + _CADDY_SHUTDOWN_TIMEOUT_SECONDS
        )
        try:
            process = self._loaded_caddy_process(shutdown_deadline)
        except InstallError as error:
            raise _CaddyTransitionError(
                "Caddy pre-shutdown process inspection failed",
                stage="pre-bootout",
            ) from error
        try:
            bootout = self._run_before_deadline(
                ["launchctl", "bootout", self._launch_service()],
                shutdown_deadline,
                message="Caddy shutdown command timed out",
            )
        except InstallError as error:
            raise _CaddyTransitionError(
                "Caddy shutdown command failed",
                process,
                stage="bootout-failed",
            ) from error
        if bootout.returncode != 0:
            raise _CaddyTransitionError(
                "Caddy shutdown phase failed",
                process,
                stage="bootout-failed",
            )
        try:
            stopped = self._wait_for_caddy_stopped(process, shutdown_deadline)
        except InstallError as error:
            raise _CaddyTransitionError(
                "Caddy shutdown process inspection failed",
                process,
                stage="post-bootout",
            ) from error
        if not stopped:
            raise _CaddyTransitionError(
                "Caddy shutdown phase timed out",
                process,
                stage="post-bootout",
            )
        try:
            bootstrapped = self._bootstrap_caddy_job()
        except InstallError as error:
            raise _CaddyTransitionError(
                "Caddy bootstrap command failed",
                stage="post-bootout",
            ) from error
        if not bootstrapped:
            raise _CaddyTransitionError(
                "Caddy bootstrap phase failed",
                stage="post-bootout",
            )

    def _restore_loaded_caddy_job(
        self,
        interrupted_process: _CaddyProcessIdentity | None = None,
        *,
        preserve_loaded_process: bool = False,
    ) -> None:
        recovery_deadline = (
            self.monotonic() + _CADDY_RECOVERY_TIMEOUT_SECONDS
        )
        if self._caddy_loaded(recovery_deadline):
            process = self._loaded_caddy_process(recovery_deadline)
            if preserve_loaded_process and process == interrupted_process:
                return
            bootout = self._run_before_deadline(
                ["launchctl", "bootout", self._launch_service()],
                recovery_deadline,
                message="Caddy recovery shutdown command timed out",
            )
            if bootout.returncode != 0:
                raise InstallError("Caddy recovery shutdown phase failed")
            if not self._wait_for_caddy_stopped(process, recovery_deadline):
                raise InstallError("Caddy recovery shutdown phase timed out")
        elif interrupted_process is not None:
            if not self._wait_for_caddy_stopped(
                interrupted_process, recovery_deadline
            ):
                raise InstallError("Caddy recovery shutdown phase timed out")
        if not self._bootstrap_caddy_job():
            raise InstallError("Caddy recovery bootstrap phase failed")
        self._wait_for_caddy_process("Caddy recovery process did not start")

    def _wait_for_caddy_process(self, message: str) -> _CaddyProcessIdentity:
        process_deadline = self.monotonic() + _CADDY_INSPECTION_TIMEOUT_SECONDS
        while True:
            process = self._loaded_caddy_process(process_deadline)
            if process is not None:
                return process
            remaining = process_deadline - self.monotonic()
            if remaining <= 0:
                raise InstallError(message)
            self.sleep(min(_CADDY_TRANSITION_POLL_SECONDS, remaining))
            if self.monotonic() >= process_deadline:
                raise InstallError(message)

    def _wait_for_caddy_ingress(
        self, process: _CaddyProcessIdentity, *, prepared: bool = False,
    ) -> None:
        deadline = self.monotonic() + _CADDY_INGRESS_READINESS_TIMEOUT_SECONDS
        verify = self.ingress_verifier.verify_prepared if prepared else self.ingress_verifier.verify_loaded
        while True:
            try:
                verify(
                    self.registry, process.pid, timeout=deadline - self.monotonic(),
                )
                if self.monotonic() < deadline:
                    return
            except IngressVerificationError:
                pass
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise IngressVerificationError("Caddy ingress did not become ready")
            self.sleep(min(_CADDY_TRANSITION_POLL_SECONDS, remaining))
            if self.monotonic() >= deadline:
                raise IngressVerificationError("Caddy ingress did not become ready")

    def _stop_loaded_caddy_job(self) -> None:
        """Undo only a candidate job that this publication replaced from missing state."""
        deadline = self.monotonic() + _CADDY_RECOVERY_TIMEOUT_SECONDS
        if not self._caddy_loaded(deadline):
            return
        process = self._loaded_caddy_process(deadline)
        bootout = self._run_before_deadline(
            ["launchctl", "bootout", self._launch_service()],
            deadline,
            message="Caddy recovery shutdown command timed out",
        )
        if bootout.returncode != 0:
            raise InstallError("cannot stop failed Caddy LaunchAgent")
        if not self._wait_for_caddy_stopped(process, deadline):
            raise InstallError("cannot stop failed Caddy LaunchAgent")

    def _wait_for_caddy_stopped(
        self,
        process: _CaddyProcessIdentity | None,
        deadline: float,
    ) -> bool:
        while True:
            job_missing = not self._caddy_loaded(deadline)
            process_exited = (
                process is None
                or not self._same_process_is_running(process, deadline)
            )
            if job_missing and process_exited:
                return True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            self.sleep(min(_CADDY_TRANSITION_POLL_SECONDS, remaining))

    def _bootstrap_caddy_job(self, deadline: float | None = None) -> bool:
        if deadline is None:
            deadline = self.monotonic() + _CADDY_BOOTSTRAP_TIMEOUT_SECONDS
        bootstrap_command = [
            "launchctl",
            "bootstrap",
            self._launch_domain(),
            str(self.caddy_plist),
        ]
        while True:
            bootstrap = self._run_before_deadline(
                bootstrap_command,
                deadline,
                message="Caddy bootstrap command timed out",
            )
            if bootstrap.returncode == 0:
                return self._wait_for_caddy_loaded(deadline)
            if (
                bootstrap.returncode not in (5, errno.EALREADY)
                or self._caddy_loaded(deadline)
            ):
                return False
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            self.sleep(min(_CADDY_TRANSITION_POLL_SECONDS, remaining))
            if self.monotonic() >= deadline:
                return False

    def _wait_for_caddy_loaded(self, deadline: float) -> bool:
        while True:
            if self._caddy_loaded(deadline):
                return True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            self.sleep(min(_CADDY_TRANSITION_POLL_SECONDS, remaining))

    def _loaded_caddy_process(
        self, deadline: float | None = None
    ) -> _CaddyProcessIdentity | None:
        if deadline is None:
            deadline = self.monotonic() + _CADDY_INSPECTION_TIMEOUT_SECONDS
        result = self._run_before_deadline(
            ["launchctl", "print", self._launch_service()],
            deadline,
            message="Caddy process inspection timed out",
        )
        if result.returncode != 0:
            raise InstallError("cannot identify loaded Caddy process")
        matches = _CADDY_PID.findall(result.stdout)
        if len(matches) > 1:
            raise InstallError("cannot identify loaded Caddy process")
        if not matches:
            if _CADDY_RUNNING.search(result.stdout):
                raise InstallError("cannot identify loaded Caddy process")
            return None
        return self._process_identity(int(matches[0]), deadline)

    def _process_identity(
        self, pid: int, deadline: float | None = None
    ) -> _CaddyProcessIdentity | None:
        if deadline is None:
            deadline = self.monotonic() + _CADDY_INSPECTION_TIMEOUT_SECONDS
        result = self._run_before_deadline(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            deadline,
            message="Caddy process inspection timed out",
        )
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return None
        started = result.stdout.strip()
        if result.returncode != 0 or not started or "\n" in started:
            raise InstallError("cannot inspect managed Caddy process")
        return _CaddyProcessIdentity(pid, started)

    def _same_process_is_running(
        self, process: _CaddyProcessIdentity, deadline: float
    ) -> bool:
        current = self._process_identity(process.pid, deadline)
        return current == process

    def _candidate(self, content: bytes, mode: int) -> Path:
        descriptor = -1
        path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.caddyfile.name}.",
                suffix=".candidate",
                dir=self.caddyfile.parent,
            )
            path = Path(name)
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            return path
        except OSError as error:
            if path is not None:
                path.unlink(missing_ok=True)
            raise InstallError("cannot prepare Caddy validation candidate") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _caddy_loaded(self, deadline: float | None = None) -> bool:
        if deadline is None:
            deadline = self.monotonic() + _CADDY_INSPECTION_TIMEOUT_SECONDS
        result = self._run_before_deadline(
            ["launchctl", "print", f"gui/{os.getuid()}/{_CADDY_LABEL}"],
            deadline,
            message="Caddy LaunchAgent inspection timed out",
        )
        if result.returncode == 0:
            return True
        missing = (
            f'Could not find service "{_CADDY_LABEL}" '
            f"in domain for user gui: {os.getuid()}"
        )
        accepted_missing = (
            missing,
            f"{missing}\n",
            f"Bad request.\n{missing}",
            f"Bad request.\n{missing}\n",
        )
        if result.returncode in (3, 113) and result.stderr in accepted_missing:
            return False
        raise InstallError("cannot inspect loaded Caddy LaunchAgent")

    def _power_messages(self) -> tuple[str, ...]:
        result = self.run(["pmset", "-g", "custom"])
        if result.returncode != 0:
            raise InstallError("cannot inspect AC power sleep setting")
        match = _AC_SECTION.search(result.stdout)
        if match is None:
            raise InstallError("cannot find AC power sleep setting")
        following = result.stdout[match.end() :]
        next_section = re.search(r"^[^\s].*:\s*$", following, re.MULTILINE)
        if next_section is not None:
            following = following[: next_section.start()]
        sleep = _SLEEP_SETTING.search(following)
        if sleep is None:
            raise InstallError("cannot find AC power sleep setting")
        return (_SLEEP_ADVICE,) if int(sleep.group(1)) != 0 else ()
