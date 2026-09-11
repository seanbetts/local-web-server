"""The deliberately small, everyday local-web command-line interface."""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .app_activation import (
    AppActivationError,
    AppActivationPlan,
    AppActivationResult,
    AppActivator,
)
from .app_check import AppCheckError, AppChecker
from .app_generator import AppGenerationError, AppGenerator, CreateAppRequest
from .app_doctor import AppDoctor
from .app_identity import (
    AppIdentityCatalogue,
    IdentityAssignment,
    IdentityCatalogueError,
    IdentityReport,
)
from .app_registration import AppRegistrar, AppRegistrationError
from .app_update import AppUpdateError, AppUpdater
from .app_update_models import AppUpdatePlan, AppUpdateRequest
from .config import ConfigError, load_registry, load_registry_bytes
from .deploy import DeploymentManager, HealthCheckFailed, RollbackUnavailable
from .fleet_update import FleetUpdater
from .fleet_update_models import FleetAppPlan, FleetAppResult
from .git_build import BuildFailed, BuildOutputInvalid
from .host_commands import HostCommands, HostCommandError, HostInitialBackupError
from .host_profile import HostProfileError, HostProfilePaths, resolve_host_profile
from .host_profile_store import HostProfileStore, HostProfileStoreError
from .host_profile_backup import (
    HostProfileBackupError,
    MAX_BACKUP_BYTES,
    MAX_PROFILE_BYTES,
    MAX_REVISION_BYTES,
    MAX_REVISIONS,
)
from .install import load_main_manifests
from .models import HostRegistry
from .runtime import DeploymentBusy
from .service_command_migration import (
    ServiceCommandMigrationError,
    ServiceCommandMigrationPlan,
    ServiceCommandMigrator,
)
from .public_base_path_migration import (
    PublicBasePathMigrationError,
    PublicBasePathMigrationPlan,
    PublicBasePathMigrator,
)
from .services import ServiceState
from .status import AppStatus, StatusCollector


_PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
_INVALID_PRIVATE_PROFILE = (
    "private host profile is invalid; run local-web host restore"
)
_HOST_PROFILE_LIMITS = {
    "max_profile_bytes": MAX_PROFILE_BYTES,
    "max_revision_bytes": MAX_REVISION_BYTES,
    "max_revisions": MAX_REVISIONS,
    "max_total_bytes": MAX_BACKUP_BYTES,
}


def _load_default_host_profile() -> tuple[Path, HostRegistry]:
    profile = resolve_host_profile(_PLATFORM_REPOSITORY)
    try:
        paths = HostProfilePaths.for_repository(
            _PLATFORM_REPOSITORY, allow_public_scaffold=True
        )
        if paths.profile != profile:
            raise HostProfileError(_INVALID_PRIVATE_PROFILE)
        snapshot = HostProfileStore(
            paths, allow_public_scaffold=True
        ).read_snapshot(**_HOST_PROFILE_LIMITS)
        registry = load_registry_bytes(snapshot.profile_bytes)
    except (ConfigError, HostProfileStoreError, UnicodeError) as error:
        raise HostProfileError(_INVALID_PRIVATE_PROFILE) from error
    return profile, registry


def _registration_profile_store(profile: Path) -> HostProfileStore:
    paths = HostProfilePaths.for_repository(_PLATFORM_REPOSITORY)
    if paths.profile != Path(profile):
        raise HostProfileError(_INVALID_PRIVATE_PROFILE)
    return HostProfileStore(paths)


class _CommandArgumentError(ValueError):
    """A bounded replacement for argparse's caller-controlled diagnostics."""


class _BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _CommandArgumentError("invalid command arguments")


def build_parser(*, _bounded_errors: bool = False) -> argparse.ArgumentParser:
    parser_class = (
        _BoundedArgumentParser if _bounded_errors else argparse.ArgumentParser
    )
    parser = parser_class(prog="local-web")
    commands = parser.add_subparsers(dest="command", required=True)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("app")
    deploy.add_argument("--from-hook", action="store_true", help=argparse.SUPPRESS)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("app")
    theme = commands.add_parser("theme")
    theme_commands = theme.add_subparsers(dest="theme_command", required=True)
    theme_commands.add_parser("rollback")
    app = commands.add_parser("app")
    app_commands = app.add_subparsers(dest="app_command", required=True)
    create = app_commands.add_parser("create")
    create.add_argument("app_id")
    _add_generation_arguments(create, destination_option=True)
    init = app_commands.add_parser("init")
    init.add_argument("directory", type=Path)
    _add_generation_arguments(init, destination_option=False)
    doctor = app_commands.add_parser("doctor")
    doctor.add_argument("--repository", type=Path, default=Path("."))
    doctor.add_argument("--json", action="store_true")
    check = app_commands.add_parser("check")
    check.add_argument("--repository", type=Path, default=Path("."))
    update = app_commands.add_parser("update")
    update.add_argument("--repository", type=Path, default=Path("."))
    update.add_argument("--capability", action="append", default=[])
    update.add_argument("--foundation", choices=("react-vite",))
    update.add_argument("--dry-run", action="store_true")
    register = app_commands.add_parser("register")
    register.add_argument("--repository", type=Path, default=Path("."))
    register.add_argument("--port", type=int)
    registration_mode = register.add_mutually_exclusive_group()
    registration_mode.add_argument("--dry-run", action="store_true")
    registration_mode.add_argument("--apply", action="store_true")
    identity = app_commands.add_parser("identity")
    identity.add_argument("--search")
    identity.add_argument("--json", action="store_true")
    activate = app_commands.add_parser("activate")
    activate.add_argument("--repository", type=Path, default=Path("."))
    activate.add_argument("--apply", action="store_true")
    activate.add_argument("--json", action="store_true")
    migrate = app_commands.add_parser("migrate-service-command")
    migrate.add_argument("--repository", type=Path, default=Path("."))
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--json", action="store_true")
    public_base = app_commands.add_parser("migrate-public-base-path")
    public_base.add_argument("--repository", type=Path, default=Path("."))
    public_base.add_argument("--apply", action="store_true")
    public_base.add_argument("--json", action="store_true")
    apps = commands.add_parser("apps")
    apps_commands = apps.add_subparsers(dest="apps_command", required=True)
    fleet_update = apps_commands.add_parser("update")
    fleet_mode = fleet_update.add_mutually_exclusive_group(required=True)
    fleet_mode.add_argument("--dry-run", action="store_true")
    fleet_mode.add_argument("--apply", action="store_true")
    commands.add_parser("status")
    host = commands.add_parser("host")
    host_commands = host.add_subparsers(dest="host_command", required=True)
    host_init = host_commands.add_parser("init")
    host_init.add_argument("--from", dest="source", type=Path, required=True)
    host_init.add_argument("--apply", action="store_true")
    host_migrate = host_commands.add_parser("migrate-registry")
    host_migrate.add_argument("--platform-repository", type=Path)
    host_migrate.add_argument("--apply", action="store_true")
    host_status = host_commands.add_parser("status")
    host_status.add_argument("--json", action="store_true")
    host_backup = host_commands.add_parser("backup")
    host_backup.add_argument("--output", type=Path)
    host_restore = host_commands.add_parser("restore")
    host_restore.add_argument("--from", dest="source", type=Path, required=True)
    host_restore.add_argument("--replace", action="store_true")
    host_restore.add_argument("--apply", action="store_true")
    host_recover = host_commands.add_parser("recover")
    host_recover.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    command_argv = sys.argv[1:] if argv is None else argv
    bounded_errors = (
        (len(command_argv) >= 2
        and command_argv[0] == "app"
        and command_argv[1]
        in ("migrate-service-command", "migrate-public-base-path"))
        or (len(command_argv) >= 1 and command_argv[0] in ("apps", "host"))
    )
    try:
        args = build_parser(_bounded_errors=bounded_errors).parse_args(command_argv)
        if args.command == "host":
            try:
                return _run_host(args)
            except Exception as error:
                message = (
                    "private profile initialized; backup incomplete; run local-web host backup"
                    if isinstance(error, HostInitialBackupError)
                    else "private host command failed"
                )
                print(f"local-web: {message}", file=sys.stderr)
                return 2
        if args.command == "apps":
            return _run_fleet_update(args)
        if args.command == "app":
            if args.app_command == "identity":
                registry_path, _registry = _load_default_host_profile()
                report = identity_report(args.search, registry_path)
                if args.json:
                    print(
                        json.dumps(
                            report.as_dict(), sort_keys=True, separators=(",", ":")
                        )
                    )
                else:
                    _print_identity_report(report)
                return 0
            if args.app_command == "activate":
                registry_path, _registry = _load_default_host_profile()
                activator = AppActivator(registry_path=registry_path)
                if args.apply:
                    result = activator.activate(args.repository)
                    if args.json:
                        print(
                            json.dumps(
                                _activation_result_payload(result),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                    else:
                        _print_activation_result(result)
                else:
                    plan = activator.preview(args.repository)
                    if args.json:
                        print(
                            json.dumps(
                                _activation_plan_payload(plan),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                    else:
                        _print_activation_plan(plan)
                return 0
            if args.app_command == "migrate-service-command":
                registry_path, _registry = _load_default_host_profile()
                migrator = ServiceCommandMigrator(registry_path=registry_path)
                result = migrator.migrate(args.repository) if args.apply else None
                plan = (
                    result.plan
                    if result is not None
                    else migrator.preview(args.repository)
                )
                if args.json:
                    payload = _service_command_migration_payload(plan)
                    if result is not None:
                        payload["registryRevision"] = result.registry_revision
                    print(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    _print_service_command_migration_plan(plan)
                return 0
            if args.app_command == "migrate-public-base-path":
                registry_path, _registry = _load_default_host_profile()
                migrator = PublicBasePathMigrator(registry_path=registry_path)
                result = migrator.migrate(args.repository) if args.apply else None
                plan = (
                    result.plan
                    if result is not None
                    else migrator.preview(args.repository)
                )
                if args.json:
                    payload = _public_base_path_migration_payload(plan)
                    if result is not None:
                        payload["registryRevision"] = result.registry_revision
                    print(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    _print_public_base_path_migration_plan(plan)
                return 0
            if args.app_command == "doctor":
                report = AppDoctor().inspect(args.repository)
                if args.json:
                    print(report.as_json())
                elif report.diagnostics:
                    for diagnostic in report.diagnostics:
                        print(
                            f"{diagnostic.severity.upper()} {diagnostic.code}: "
                            f"{diagnostic.message} {diagnostic.remedy}"
                        )
                else:
                    print(f"{report.app}: compatible")
                return 0 if report.compatible else 1
            if args.app_command == "check":
                _load_default_host_profile()
                report = AppChecker().check(args.repository)
                print(f"{report.app}: checks passed")
                return 0
            if args.app_command == "update":
                request = _update_request(args)
                updater = AppUpdater()
                if args.dry_run:
                    plan = updater.preview(request)
                    _print_update_plan(plan, action="would update")
                else:
                    result = updater.update(request)
                    _print_update_plan(result.plan, action="updated")
                    recovery = "completed" if result.recovered else "not required"
                    print(f"recovery: {recovery}")
                return 0
            if args.app_command == "register":
                registry_path, _registry = _load_default_host_profile()
                profile_store = _registration_profile_store(registry_path)
                result = AppRegistrar().register(
                    args.repository,
                    registry_path,
                    profile_store=profile_store,
                    dry_run=args.dry_run,
                    port=args.port,
                )
                action = "would register" if result.dry_run else "registered"
                print(f"{action} {result.app_id} at {result.route}")
                return 0
            request = _generation_request(args)
            generator = AppGenerator()
            if args.dry_run:
                preview = (
                    generator.preview_init(request)
                    if args.app_command == "init"
                    else generator.preview(request)
                )
                print(f"destination: {preview.destination}")
                print(f"route: {preview.route}")
                print(f"icon: {preview.icon}")
                print(f"accent: {preview.accent}")
                print(f"capabilities: {','.join(preview.capabilities) or '-'}")
                print(f"kind: {preview.kind}")
                print(f"ui: {preview.ui_version}")
                print(f"template: {preview.template_version}")
                return 0
            result = (
                generator.init(request)
                if args.app_command == "init"
                else generator.create(request)
            )
            action = "initialised" if args.app_command == "init" else "created"
            print(
                f"{action} {request.app_id} at {result.destination} "
                f"ui={result.ui_version} commit={result.commit}"
            )
            return 0
        _registry_path, registry = _load_default_host_profile()
        if args.command == "deploy":
            result = DeploymentManager(registry).deploy(args.app, from_hook=args.from_hook)
            print(result.message)
            return 0
        if args.command == "rollback":
            result = DeploymentManager(registry).rollback(args.app)
            print(result.message)
            return 0
        if args.command == "theme":
            from .theme import ThemeStore

            digest = ThemeStore(registry.runtime_root).rollback()
            print(f"platform theme rolled back to {digest}")
            return 0

        collector = StatusCollector(registry)
        statuses = collector.collect()
        if collector.caddy_state is None:
            raise ValueError("Caddy state was not collected")
        print(f"caddy: {collector.caddy_state.value}")
        for status in statuses:
            print(_format_status(status))
        return (
            0
            if collector.caddy_state is ServiceState.RUNNING
            and all(status.healthy for status in statuses)
            else 1
        )
    except (
        ConfigError,
        DeploymentBusy,
        BuildFailed,
        BuildOutputInvalid,
        HealthCheckFailed,
        RollbackUnavailable,
        AppGenerationError,
        AppRegistrationError,
        AppCheckError,
        AppUpdateError,
        AppActivationError,
        ServiceCommandMigrationError,
        PublicBasePathMigrationError,
        IdentityCatalogueError,
        HostProfileError,
        HostProfileStoreError,
        HostProfileBackupError,
        HostCommandError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        if len(command_argv) >= 1 and command_argv[0] == "host":
            message = "invalid command arguments" if isinstance(error, _CommandArgumentError) else "private host command failed"
            print(f"local-web: {message}", file=sys.stderr)
        else:
            print(f"local-web: {error}", file=sys.stderr)
        return 2


def _run_host(args: argparse.Namespace) -> int:
    commands = HostCommands(args.platform_repository) if args.host_command == "migrate-registry" else HostCommands()
    if args.host_command == "status":
        payload = commands.status().as_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        else:
            for key, value in payload.items():
                print(f"{key}={json.dumps(value, ensure_ascii=True, separators=(',', ':'))}")
        return 0
    if args.host_command == "backup":
        result = commands.backup(args.output)
    else:
        if args.host_command == "init":
            plan = commands.preview_init(args.source)
        elif args.host_command == "migrate-registry":
            plan = commands.preview_migration()
        elif args.host_command == "restore":
            plan = commands.preview_restore(args.source, replace=args.replace)
        else:
            plan = commands.preview_recovery()
        result = commands.apply(plan) if args.apply else plan
    # No dataclass serialization: opaque exact-state tokens contain private
    # bytes. Only these explicit fixed public payloads may cross the CLI.
    print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0


def _format_status(status: AppStatus) -> str:
    state = "CURRENT" if status.healthy else "UNHEALTHY"
    if status.stale:
        state = "STALE"
    elif status.deployed_commit is None:
        state = "NOT DEPLOYED"
    parts = [
        f"{status.app_id}: {state}",
        f"main={status.main_commit}",
        f"deployed={status.deployed_commit or '-'}",
        f"service={status.service_state.value if status.service_state else '-'}",
        f"internal={_health_label(status.internal_health)}",
        f"frontend={_health_label(status.frontend_health)}",
        f"backend={_health_label(status.backend_health)}",
        f"log={status.latest_log or '-'}",
    ]
    return " ".join(parts)


def _run_fleet_update(args: argparse.Namespace) -> int:
    registry_path, _registry = _load_default_host_profile()
    try:
        updater = FleetUpdater()
        if args.dry_run:
            plan = updater.preview(registry_path)
            lines = tuple(_format_fleet_plan(app) for app in plan.apps)
            for line in lines:
                print(line)
            return 0
        result = updater.apply(registry_path)
        lines = tuple(_format_fleet_result(app) for app in result.apps)
        for line in lines:
            print(line)
        print(_format_fleet_summary(result.apps))
        return 0 if all(app.status in {"UPDATED", "CURRENT"} for app in result.apps) else 1
    except Exception:
        print("local-web: fleet application update failed", file=sys.stderr)
        return 2


def _format_fleet_plan(app: FleetAppPlan) -> str:
    return (
        f"{app.app_id} {app.status} "
        f"ui={_format_fleet_version(app.current_ui_version)}->{_format_fleet_version(app.target_ui_version)} "
        f"source={_format_fleet_commit(app.source_head)} "
        f"deployed={_format_fleet_commit(app.deployed_commit)} "
        f"paths={_format_fleet_paths(app.paths)} "
        f"digest={_format_fleet_digest(app.target_ui_sha256)}"
    )


def _format_fleet_result(app: FleetAppResult) -> str:
    return (
        f"{app.app_id} {app.status} "
        f"ui={_format_fleet_version(app.current_ui_version)}->{_format_fleet_version(app.target_ui_version)} "
        f"source={_format_fleet_commit(app.source_head)} "
        f"deployed={_format_fleet_commit(app.deployed_commit)} "
        f"paths={_format_fleet_paths(app.paths)} "
        f"verified={'yes' if app.verified else 'no'}"
    )


def _format_fleet_summary(apps: tuple[FleetAppResult, ...]) -> str:
    counts = {
        "UPDATED": 0,
        "CURRENT": 0,
        "SKIPPED": 0,
        "BLOCKED": 0,
        "FAILED_RECOVERED": 0,
        "RECOVERY_FAILED": 0,
    }
    for app in apps:
        counts[app.status] += 1
    return (
        f"summary updated={counts['UPDATED']} current={counts['CURRENT']} "
        f"skipped={counts['SKIPPED']} blocked={counts['BLOCKED']} "
        f"failed-recovered={counts['FAILED_RECOVERED']} "
        f"recovery-failed={counts['RECOVERY_FAILED']}"
    )


def _format_fleet_version(value: str | None) -> str:
    return value or "-"


def _format_fleet_commit(value: str | None) -> str:
    return value[:12] if value is not None else "-"


def _format_fleet_paths(paths: tuple[Path, ...]) -> str:
    return ",".join(path.as_posix() for path in paths) or "-"


def _format_fleet_digest(value: str | None) -> str:
    return value[:12] if value is not None else "-"


def _add_generation_arguments(
    parser: argparse.ArgumentParser, *, destination_option: bool
) -> None:
    parser.add_argument("--title", required=True)
    if destination_option:
        parser.add_argument("--destination", type=Path)
    parser.add_argument("--route")
    parser.add_argument("--icon", required=True)
    parser.add_argument("--accent", required=True)
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--kind", choices=("static", "service"), default="static")
    parser.add_argument("--dry-run", action="store_true")


def _generation_request(args: argparse.Namespace) -> CreateAppRequest:
    if args.app_command == "init":
        destination = args.directory.absolute()
        app_id = destination.name
    else:
        app_id = args.app_id
        destination = args.destination or (Path.cwd() / app_id)
    return CreateAppRequest(
        app_id=app_id,
        title=args.title,
        destination=destination,
        route=args.route or f"/{app_id}",
        icon=args.icon,
        accent=args.accent,
        capabilities=tuple(sorted(args.capability)),
        kind=args.kind,
    )


def _update_request(args: argparse.Namespace) -> AppUpdateRequest:
    capabilities = tuple(sorted(args.capability))
    if (
        len(capabilities) != len(set(capabilities))
        or any(capability != "supabase" for capability in capabilities)
    ):
        raise AppUpdateError("application update request is invalid")
    return AppUpdateRequest(
        args.repository, capabilities, foundation=args.foundation
    )


def _print_update_plan(plan: AppUpdatePlan, *, action: str) -> None:
    print(f"mode: {plan.mode}")
    if plan.foundation is not None:
        print(f"foundation: {plan.foundation}")
        print(
            "current platform contract: "
            f"{plan.current_platform_contract_version or '-'}"
        )
        print(
            "target platform contract: "
            f"{plan.target_platform_contract_version or '-'}"
        )
        print(f"current template: {plan.current_template_version or '-'}")
        print(f"target template: {plan.target_template_version or '-'}")
        print(f"current ui: {plan.current_ui_version or '-'}")
    print(f"target ui: {plan.target_ui_version}")
    print(f"digest: {plan.target_ui_sha256}")
    print(f"capabilities: {','.join(plan.capabilities) or '-'}")
    if not plan.changes:
        print(f"{action}: none")
        return
    print(f"{action}:")
    for change in plan.changes:
        print(f"  {change.path.as_posix()}")


def _health_label(result) -> str:
    if result is None:
        return "-"
    return "OK" if result.healthy else "FAIL"


def identity_report(search: str | None, registry_path: Path) -> IdentityReport:
    try:
        registry = load_registry(registry_path)
        manifests = load_main_manifests(registry)
        assignments = tuple(
            IdentityAssignment(
                app_id=host.id,
                title=manifests[host.id].title,
                icon=manifests[host.id].home.icon,
                accent=manifests[host.id].home.accent,
            )
            for host in registry.apps
        )
        return AppIdentityCatalogue.load().discover(search, assignments)
    except IdentityCatalogueError:
        raise
    except (ConfigError, OSError, RuntimeError, ValueError) as error:
        raise IdentityCatalogueError(
            "application identity discovery failed"
        ) from error


def _print_identity_report(report: IdentityReport) -> None:
    print("icons:")
    for icon in report.icons:
        print(
            f"  {icon.name} | {icon.label} | Tabler {icon.source} | "
            f"{','.join(icon.keywords)}"
        )
    print("accents:")
    for accent in report.accents:
        print(
            f"  {accent.name} | {accent.seed} | "
            f"light {accent.light} | dark {accent.dark}"
        )
    print("assignments:")
    for assignment in report.assignments:
        print(
            f"  {assignment.app_id} | {assignment.title} | "
            f"{assignment.icon} | {assignment.accent}"
        )
    print(f"reserved: {','.join(report.reserved_icons)}")


def _activation_plan_payload(plan: AppActivationPlan) -> dict[str, object]:
    return {
        "appId": plan.app_id,
        "route": plan.route,
        "kind": plan.kind,
        "port": plan.port,
        "registryChanged": plan.registry_changed,
        "stages": list(plan.stages),
    }


def _activation_result_payload(result: AppActivationResult) -> dict[str, object]:
    return {
        **_activation_plan_payload(result.plan),
        "registryRevision": result.registry_revision,
        "deployment": result.deployment.outcome,
        "verified": result.verified,
    }


def _print_activation_plan(plan: AppActivationPlan) -> None:
    print(f"would activate {plan.app_id} at {plan.route}")
    print(f"kind: {plan.kind}")
    print(f"port: {plan.port if plan.port is not None else '-'}")
    print(f"registry: {'update' if plan.registry_changed else 'current'}")
    print(f"stages: {','.join(plan.stages)}")


def _print_activation_result(result: AppActivationResult) -> None:
    print(f"activated {result.plan.app_id} at {result.plan.route}")
    print(f"port: {result.plan.port if result.plan.port is not None else '-'}")
    print(f"registry revision: {result.registry_revision or '-'}")
    print(f"deployment: {result.deployment.outcome}")
    print(f"verified: {'yes' if result.verified else 'no'}")


def _service_command_migration_payload(
    plan: ServiceCommandMigrationPlan,
) -> dict[str, object]:
    return {
        "application": plan.app_id,
        "serviceCommand": plan.command_status,
        "identity": plan.identity_status,
        "repository": plan.repository_status,
        "route": plan.route_status,
        "port": plan.port,
        "portStatus": plan.port_status,
        "serviceAction": plan.service_action,
    }


def _print_service_command_migration_plan(
    plan: ServiceCommandMigrationPlan,
) -> None:
    print(f"application: {plan.app_id}")
    print(f"service command: {plan.command_status}")
    print(f"identity: {plan.identity_status}")
    print(f"repository: {plan.repository_status}")
    print(f"route: {plan.route_status}")
    print(f"port: {plan.port} {plan.port_status}")
    print(f"service action: {plan.service_action}")


def _public_base_path_migration_payload(
    plan: PublicBasePathMigrationPlan,
) -> dict[str, object]:
    return {
        "application": plan.app_id,
        "publicBasePath": plan.public_base_path_status,
        "identity": plan.identity_status,
        "repository": plan.repository_status,
        "route": plan.route_status,
        "port": plan.port,
        "portStatus": plan.port_status,
        "serviceCommand": plan.service_command_status,
        "serviceAction": plan.service_action,
    }


def _print_public_base_path_migration_plan(
    plan: PublicBasePathMigrationPlan,
) -> None:
    print(f"application: {plan.app_id}")
    print(f"public base path: {plan.public_base_path_status}")
    print(f"identity: {plan.identity_status}")
    print(f"repository: {plan.repository_status}")
    print(f"route: {plan.route_status}")
    print(f"port: {plan.port} {plan.port_status}")
    print(f"service command: {plan.service_command_status}")
    print(f"service action: {plan.service_action}")
