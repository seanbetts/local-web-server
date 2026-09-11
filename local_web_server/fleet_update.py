"""Plan and safely apply routine platform updates across registered apps."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from .app_activation import AppActivationPlan, AppActivationResult, AppActivator
from .app_check import AppChecker
from .app_platform_commit import (
    AppGitState,
    AppPlatformCommitError,
    AppPlatformCommitter,
)
from .app_provenance import load_provenance
from .app_update import AppUpdatePlanChangedError, AppUpdater
from .app_update_models import (
    AppUpdatePlan,
    AppUpdateRequest,
    AppUpdateResult,
    FileChange,
)
from .app_update_transaction import UpdatePublisher
from .config import ConfigError, load_manifest, load_registry
from .fleet_update_models import (
    FleetAppPlan,
    FleetAppResult,
    FleetApplyStatus,
    FleetPreviewStatus,
    FleetUpdatePlan,
    FleetUpdateResult,
)
from .fleet_live_release import RuntimeLiveReleaseInspector
from .models import AppManifest, HostApp


_PROVENANCE = Path(".local-web-platform.json")
_MANIFEST = Path("local-web.json")
_EVIDENCE_UNSET = object()


def _activator_for_registry(registry_path: Path) -> AppActivator:
    return AppActivator(registry_path=registry_path)


class FleetUpdater:
    """Plan and apply independently recoverable registered-app updates."""

    def __init__(
        self,
        *,
        updater_factory: Callable[[], AppUpdater] = AppUpdater,
        checker_factory: Callable[[], AppChecker] = AppChecker,
        activator_factory: Callable[[Path], AppActivator] = _activator_for_registry,
        committer: AppPlatformCommitter | None = None,
        publisher: UpdatePublisher | None = None,
        live_inspector: RuntimeLiveReleaseInspector | None = None,
    ) -> None:
        self._updater_factory = updater_factory
        self._checker_factory = checker_factory
        self._activator_factory = activator_factory
        self._committer = committer or AppPlatformCommitter()
        self._publisher = publisher or UpdatePublisher()
        self._live_inspector = live_inspector or RuntimeLiveReleaseInspector()

    def preview(self, registry_path: Path) -> FleetUpdatePlan:
        registry = load_registry(registry_path)
        return FleetUpdatePlan(
            apps=tuple(
                self._preview_app(app, registry_path, registry.runtime_root)
                for app in registry.apps
            )
        )

    def apply(self, registry_path: Path) -> FleetUpdateResult:
        plan = self.preview(registry_path)
        results: list[FleetAppResult] = []
        for index, app in enumerate(plan.apps):
            if app.status != "READY":
                results.append(self._unchanged_result(app))
                continue
            result = self._apply_app(app, registry_path)
            results.append(result)
            if result.status == "RECOVERY_FAILED":
                results.extend(
                    self._aborted_result(remaining)
                    for remaining in plan.apps[index + 1 :]
                )
                break
        return FleetUpdateResult(tuple(results))

    def _apply_app(self, app: FleetAppPlan, registry_path: Path) -> FleetAppResult:
        update_plan = app._update_plan
        expected_activation = app._activation_plan
        if (
            update_plan is None
            or expected_activation is None
            or app.source_head is None
            or app.deployed_commit is None
            or app._runtime_root is None
        ):
            return self._apply_result(app, "BLOCKED", reason="plan-changed")
        request = AppUpdateRequest(repository=app.repository)
        updater = self._updater_factory()
        try:
            repeated = updater.preview(request)
            repeated_activation = self._activator_factory(registry_path).preview(
                app.repository
            )
            state = self._committer.inspect_clean_main(app.repository)
            deployed_commit = self._live_inspector.inspect(
                app._runtime_root, app.app_id
            )
        except Exception:
            return self._apply_result(app, "BLOCKED", reason="plan-changed")
        if (
            type(repeated) is not AppUpdatePlan
            or repeated != update_plan
            or type(repeated_activation) is not AppActivationPlan
            or repeated_activation != expected_activation
            or repeated_activation.registry_changed is not False
            or state.repository != app.repository
            or state.head != app.source_head
            or deployed_commit != app.deployed_commit
        ):
            reason = (
                "live-release-changed"
                if deployed_commit != app.deployed_commit
                else "plan-changed"
            )
            return self._apply_result(app, "BLOCKED", reason=reason)

        try:
            update = updater.update(request, expected_plan=update_plan)
            if type(update) is not AppUpdateResult or update.plan != update_plan:
                raise ValueError
        except AppUpdatePlanChangedError:
            if self._is_exact_state(app.repository, app.source_head) and self._is_exact_live(
                app, app.deployed_commit
            ):
                return self._apply_result(app, "BLOCKED", reason="plan-changed")
            return self._apply_result(
                app, "RECOVERY_FAILED", reason="recovery-failed"
            )
        except Exception:
            if self._is_exact_state(app.repository, app.source_head) and self._is_exact_live(
                app, app.deployed_commit
            ):
                return self._apply_result(
                    app,
                    "FAILED_RECOVERED",
                    source_head=app.source_head,
                    deployed_commit=app.deployed_commit,
                    verified=True,
                    reason="update-failed",
                )
            return self._apply_result(
                app, "RECOVERY_FAILED", reason="recovery-failed"
            )

        try:
            update_commit = self._committer.commit_update(
                app.repository, update_plan, app.source_head
            )
        except Exception:
            return self._recover_uncommitted(app, update_plan)

        try:
            self._checker_factory().check(app.repository)
        except Exception:
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "check-failed",
            )

        try:
            manifest = load_manifest(app.repository / _MANIFEST)
            activator = self._activator_factory(registry_path)
            activation_plan = activator.preview(app.repository)
            self._require_activation_plan(
                activation_plan, manifest, expected_activation
            )
        except Exception:
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "activation-preview-failed",
            )

        try:
            activation = activator.activate(
                app.repository,
                expected_plan=expected_activation,
                expected_source_commit=update_commit,
                expected_former_live_commit=app.deployed_commit,
            )
        except Exception:
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "activation-failed",
            )
        if not self._activation_matches(
            activation, update_commit, manifest, activation_plan
        ):
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "verification-failed",
            )
        if not self._is_exact_state(app.repository, update_commit):
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "verification-failed",
            )
        if not self._is_exact_live(app, update_commit):
            return self._recover_app(
                app,
                update_plan,
                update_commit,
                registry_path,
                "verification-failed",
            )
        return self._apply_result(
            app,
            "UPDATED",
            source_head=update_commit,
            deployed_commit=update_commit,
            verified=True,
        )

    def _recover_uncommitted(
        self, app: FleetAppPlan, update_plan: AppUpdatePlan
    ) -> FleetAppResult:
        inverse = self._inverse(update_plan)
        try:
            self._publisher.publish(
                app.repository,
                inverse,
                lambda: self._require_published_bytes(app.repository, inverse),
            )
        except Exception:
            return self._apply_result(
                app, "RECOVERY_FAILED", reason="recovery-failed"
            )
        if app.source_head is None or not self._is_exact_state(
            app.repository, app.source_head
        ) or not self._is_exact_live(app, app.deployed_commit):
            return self._apply_result(
                app, "RECOVERY_FAILED", reason="recovery-failed"
            )
        return self._apply_result(
            app,
            "FAILED_RECOVERED",
            source_head=app.source_head,
            deployed_commit=app.deployed_commit,
            verified=True,
            reason="commit-failed",
        )

    def _recover_app(
        self,
        app: FleetAppPlan,
        update_plan: AppUpdatePlan,
        update_commit: str,
        registry_path: Path,
        failure_reason: str,
    ) -> FleetAppResult:
        recovery_live_commit = self._inspect_live_or_none(app)
        if recovery_live_commit not in {app.deployed_commit, update_commit}:
            return self._apply_result(
                app,
                "RECOVERY_FAILED",
                source_head=update_commit,
                deployed_commit=recovery_live_commit,
                reason="recovery-failed",
            )
        restoration_commit: str | None = None
        try:
            inverse = self._inverse(update_plan)
            self._publisher.publish(
                app.repository,
                inverse,
                lambda: self._require_published_bytes(app.repository, inverse),
            )
            restoration_commit = self._committer.commit_restoration(
                app.repository, update_plan, update_commit
            )
            self._checker_factory().check(app.repository)
            manifest = load_manifest(app.repository / _MANIFEST)
            activator = self._activator_factory(registry_path)
            activation_plan = activator.preview(app.repository)
            self._require_activation_plan(
                activation_plan, manifest, app._activation_plan
            )
            activation = activator.activate(
                app.repository,
                expected_plan=app._activation_plan,
                expected_source_commit=restoration_commit,
                expected_former_live_commit=recovery_live_commit,
            )
            if not self._activation_matches(
                activation, restoration_commit, manifest, activation_plan
            ):
                raise ValueError
            if not self._is_exact_state(app.repository, restoration_commit):
                raise ValueError
            if not self._is_exact_live(app, restoration_commit):
                raise ValueError
        except Exception:
            return self._apply_result(
                app,
                "RECOVERY_FAILED",
                source_head=restoration_commit or update_commit,
                deployed_commit=self._inspect_live_or_none(app),
                reason="recovery-failed",
            )
        return self._apply_result(
            app,
            "FAILED_RECOVERED",
            source_head=restoration_commit,
            deployed_commit=restoration_commit,
            verified=True,
            reason=failure_reason,
        )

    @staticmethod
    def _inverse(plan: AppUpdatePlan) -> tuple[FileChange, ...]:
        return tuple(
            FileChange(change.path, change.after, change.before)
            for change in plan.changes
        )

    @staticmethod
    def _require_published_bytes(
        repository: Path, changes: tuple[FileChange, ...]
    ) -> None:
        for change in changes:
            target = repository / change.path
            if change.after is None:
                if target.exists() or target.is_symlink():
                    raise ValueError
            elif (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != change.after
            ):
                raise ValueError

    def _is_exact_state(self, repository: Path, commit: str) -> bool:
        try:
            state = self._committer.inspect_clean_main(repository)
        except Exception:
            return False
        return state.repository == repository and state.head == commit

    def _is_exact_live(self, app: FleetAppPlan, commit: str | None) -> bool:
        if app._runtime_root is None or commit is None:
            return False
        try:
            return (
                self._live_inspector.inspect(app._runtime_root, app.app_id)
                == commit
            )
        except Exception:
            return False

    def _inspect_live_or_none(self, app: FleetAppPlan) -> str | None:
        if app._runtime_root is None:
            return None
        try:
            return self._live_inspector.inspect(app._runtime_root, app.app_id)
        except Exception:
            return None

    @staticmethod
    def _require_activation_plan(
        activation: object,
        manifest: AppManifest,
        expected: AppActivationPlan | None = None,
    ) -> None:
        if (
            type(activation) is not AppActivationPlan
            or activation.app_id != manifest.id
            or activation.route != manifest.route
            or activation.kind != manifest.kind
            or activation.registry_changed is not False
            or (expected is not None and activation != expected)
        ):
            raise ValueError

    @classmethod
    def _activation_matches(
        cls,
        activation: object,
        commit: str,
        manifest: AppManifest,
        expected_plan: AppActivationPlan,
    ) -> bool:
        if type(activation) is not AppActivationResult:
            return False
        try:
            cls._require_activation_plan(
                activation.plan, manifest, expected_plan
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return (
            activation.verified is True
            and activation.deployment.app_id == manifest.id
            and activation.deployment.commit == commit
        )

    @staticmethod
    def _unchanged_result(app: FleetAppPlan) -> FleetAppResult:
        return FleetAppResult(
            app.app_id,
            app.status,
            app.current_ui_version,
            app.target_ui_version,
            app.source_head,
            app.deployed_commit,
            app.paths,
            (
                app.status == "CURRENT"
                and app.source_head is not None
                and app.source_head == app.deployed_commit
            ),
            app.reason,
        )

    @classmethod
    def _aborted_result(cls, app: FleetAppPlan) -> FleetAppResult:
        if app.status != "READY":
            return cls._unchanged_result(app)
        return cls._apply_result(app, "BLOCKED", reason="recovery-failed")

    @staticmethod
    def _apply_result(
        app: FleetAppPlan,
        status: FleetApplyStatus,
        *,
        source_head: str | None | object = _EVIDENCE_UNSET,
        deployed_commit: str | None | object = _EVIDENCE_UNSET,
        verified: bool = False,
        reason: str | None = None,
    ) -> FleetAppResult:
        return FleetAppResult(
            app.app_id,
            status,
            app.current_ui_version,
            app.target_ui_version,
            (
                app.source_head
                if source_head is _EVIDENCE_UNSET
                else cast(str | None, source_head)
            ),
            (
                app.deployed_commit
                if deployed_commit is _EVIDENCE_UNSET
                else cast(str | None, deployed_commit)
            ),
            app.paths,
            verified,
            reason,
        )

    def _preview_app(
        self, app: HostApp, registry_path: Path, runtime_root: Path
    ) -> FleetAppPlan:
        try:
            state = self._committer.inspect_clean_main(app.repository)
        except AppPlatformCommitError as error:
            return self._blocked(
                app,
                error.inspection_reason or "repository-unavailable",
            )

        try:
            manifest = load_manifest(state.repository / _MANIFEST)
        except (ConfigError, OSError, UnicodeError, ValueError):
            return self._blocked(
                app,
                "application-contract-invalid",
                repository=state.repository,
                source_head=state.head,
            )
        if manifest.id != app.id:
            return self._blocked(
                app,
                "application-contract-invalid",
                repository=state.repository,
                source_head=state.head,
            )
        if manifest.platform is None:
            return FleetAppPlan(
                app_id=app.id,
                status="SKIPPED",
                current_ui_version=None,
                target_ui_version=None,
                target_ui_sha256=None,
                source_head=state.head,
                deployed_commit=None,
                paths=(),
                reason="legacy-adoption-required",
                repository=state.repository,
                _update_plan=None,
            )
        try:
            load_provenance(state.repository / _PROVENANCE)
        except (OSError, UnicodeError, ValueError):
            return self._blocked(
                app,
                "application-contract-invalid",
                repository=state.repository,
                source_head=state.head,
            )

        try:
            update_plan = self._updater_factory().preview(
                AppUpdateRequest(repository=state.repository)
            )
        except Exception:
            return self._blocked(
                app,
                "update-planning-failed",
                repository=state.repository,
                source_head=state.head,
            )
        if type(update_plan) is not AppUpdatePlan or update_plan.app_id != app.id:
            return self._blocked(
                app,
                "application-contract-invalid",
                repository=state.repository,
                source_head=state.head,
            )
        if update_plan.mode == "adopt" or update_plan.foundation is not None:
            return FleetAppPlan(
                app_id=app.id,
                status="SKIPPED",
                current_ui_version=update_plan.current_ui_version,
                target_ui_version=update_plan.target_ui_version,
                target_ui_sha256=update_plan.target_ui_sha256,
                source_head=state.head,
                deployed_commit=None,
                paths=tuple(change.path for change in update_plan.changes),
                reason="foundation-adoption-required",
                repository=state.repository,
                _update_plan=None,
            )
        try:
            activation_plan = self._activator_factory(registry_path).preview(
                state.repository
            )
            if (
                type(activation_plan) is not AppActivationPlan
                or activation_plan.app_id != manifest.id
                or activation_plan.route != manifest.route
                or activation_plan.kind != manifest.kind
            ):
                raise ValueError
        except Exception:
            return self._blocked(
                app,
                "activation-planning-failed",
                repository=state.repository,
                source_head=state.head,
            )
        if activation_plan.registry_changed:
            return FleetAppPlan(
                app_id=app.id,
                status="SKIPPED",
                current_ui_version=update_plan.current_ui_version,
                target_ui_version=update_plan.target_ui_version,
                target_ui_sha256=update_plan.target_ui_sha256,
                source_head=state.head,
                deployed_commit=None,
                paths=tuple(change.path for change in update_plan.changes),
                reason="activation-transition-required",
                repository=state.repository,
                _update_plan=None,
                _activation_plan=None,
            )
        try:
            deployed_commit = self._live_inspector.inspect(runtime_root, app.id)
        except Exception:
            deployed_commit = None
        if deployed_commit != state.head:
            return FleetAppPlan(
                app_id=app.id,
                status="BLOCKED",
                current_ui_version=update_plan.current_ui_version,
                target_ui_version=update_plan.target_ui_version,
                target_ui_sha256=update_plan.target_ui_sha256,
                source_head=state.head,
                deployed_commit=deployed_commit,
                paths=tuple(change.path for change in update_plan.changes),
                reason="live-release-not-current",
                repository=state.repository,
                _update_plan=None,
                _activation_plan=None,
                _runtime_root=runtime_root,
            )
        if update_plan.mode == "current":
            return self._from_update_plan(
                app,
                state,
                update_plan,
                activation_plan,
                runtime_root,
                deployed_commit,
                "CURRENT",
            )
        if update_plan.mode == "refresh" and update_plan.changes:
            return self._from_update_plan(
                app,
                state,
                update_plan,
                activation_plan,
                runtime_root,
                deployed_commit,
                "READY",
            )
        return self._blocked(
            app,
            "update-planning-failed",
            repository=state.repository,
            source_head=state.head,
        )

    @staticmethod
    def _from_update_plan(
        app: HostApp,
        state: AppGitState,
        update_plan: AppUpdatePlan,
        activation_plan: AppActivationPlan,
        runtime_root: Path,
        deployed_commit: str,
        status: FleetPreviewStatus,
    ) -> FleetAppPlan:
        return FleetAppPlan(
            app_id=app.id,
            status=status,
            current_ui_version=update_plan.current_ui_version,
            target_ui_version=update_plan.target_ui_version,
            target_ui_sha256=update_plan.target_ui_sha256,
            source_head=state.head,
            deployed_commit=deployed_commit,
            paths=tuple(change.path for change in update_plan.changes),
            reason=None,
            repository=state.repository,
            _update_plan=update_plan,
            _activation_plan=activation_plan,
            _runtime_root=runtime_root,
        )

    @staticmethod
    def _blocked(
        app: HostApp,
        reason: str,
        *,
        repository: Path | None = None,
        source_head: str | None = None,
    ) -> FleetAppPlan:
        return FleetAppPlan(
            app_id=app.id,
            status="BLOCKED",
            current_ui_version=None,
            target_ui_version=None,
            target_ui_sha256=None,
            source_head=source_head,
            deployed_commit=None,
            paths=(),
            reason=reason,
            repository=repository or app.repository,
            _update_plan=None,
        )
