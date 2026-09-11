import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_activation import AppActivationPlan, AppActivationResult
from local_web_server.app_platform_commit import (
    AppGitState,
    AppPlatformCommitError,
    AppPlatformCommitter,
)
from local_web_server.app_provenance import (
    AppProvenance,
    UiArtifactReference,
    render_provenance,
)
from local_web_server.app_update import AppUpdateError, AppUpdatePlanChangedError
from local_web_server.app_update_models import (
    AppUpdatePlan,
    AppUpdateRequest,
    AppUpdateResult,
    FileChange,
)
from local_web_server.app_update_transaction import UpdatePublisher
from local_web_server.deploy import DeploymentResult
from local_web_server.fleet_update import FleetUpdater
from local_web_server.fleet_live_release import (
    LiveReleaseInspectionError,
    RuntimeLiveReleaseInspector,
)
from local_web_server.runtime import RuntimeLayout


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.read_bytes(),
                path.stat().st_mode,
            )
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
    )


class _Updater:
    def __init__(self, plans: dict[Path, AppUpdatePlan], failures: set[Path]) -> None:
        self.plans = plans
        self.failures = failures
        self.requests: list[AppUpdateRequest] = []
        self.update_called = False

    def preview(self, request: AppUpdateRequest) -> AppUpdatePlan:
        self.requests.append(request)
        if request.repository in self.failures:
            raise AppUpdateError("private planning detail")
        return self.plans[request.repository]

    def update(self, request: AppUpdateRequest) -> None:
        self.update_called = True
        raise AssertionError(f"unexpected update for {request.repository}")


class _Committer:
    def __init__(
        self, states: dict[Path, str], reasons: dict[Path, str] | None = None
    ) -> None:
        self.states = states
        self.reasons = reasons or {}
        self.inspected: list[Path] = []
        self.commit_called = False

    def inspect_clean_main(self, repository: Path):
        self.inspected.append(repository)
        if repository in self.reasons:
            raise AppPlatformCommitError(
                "application repository inspection failed",
                inspection_reason=self.reasons[repository],
            )
        return AppGitState(repository, self.states[repository])

    def commit_update(self, *arguments):
        self.commit_called = True
        raise AssertionError("unexpected platform commit")

    def commit_restoration(self, *arguments):
        self.commit_called = True
        raise AssertionError("unexpected platform restoration")


class _InspectionFailureCommitter:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def inspect_clean_main(self, repository: Path):
        raise AppPlatformCommitError(
            "application repository inspection failed",
            inspection_reason=self.reason,
        )


class _PlanningActivator:
    def __init__(self, changed: set[Path] | None = None) -> None:
        self.changed = changed or set()
        self.calls: list[Path] = []

    def preview(self, repository: Path) -> AppActivationPlan:
        self.calls.append(repository)
        manifest = json.loads((repository / "local-web.json").read_text())
        return AppActivationPlan(
            manifest["id"],
            manifest["route"],
            manifest["kind"],
            52000 if manifest["kind"] == "service" else None,
            repository in self.changed,
        )


class _LiveInspector:
    def __init__(
        self,
        repositories: dict[str, Path],
        commits: dict[Path, str | None | Exception],
    ) -> None:
        self.repositories = repositories
        self.commits = commits
        self.calls: list[str] = []

    def inspect(self, _runtime_root: Path, app_id: str) -> str | None:
        self.calls.append(app_id)
        value = self.commits[self.repositories[app_id]]
        if isinstance(value, Exception):
            raise value
        return value


class _ApplyingUpdater:
    def __init__(
        self,
        plans: dict[Path, tuple[AppUpdatePlan, ...]],
        publisher,
        events: list[tuple],
        *,
        fail_update: set[Path] | None = None,
        before_preview: dict[tuple[Path, int], object] | None = None,
    ) -> None:
        self.plans = plans
        self.publisher = publisher
        self.events = events
        self.fail_update = fail_update or set()
        self.before_preview = before_preview or {}
        self.preview_counts: dict[Path, int] = {}

    def preview(self, request: AppUpdateRequest) -> AppUpdatePlan:
        count = self.preview_counts.get(request.repository, 0)
        self.preview_counts[request.repository] = count + 1
        callback = self.before_preview.get((request.repository, count + 1))
        if callback is not None:
            callback()
        choices = self.plans[request.repository]
        plan = choices[min(count, len(choices) - 1)]
        self.events.append(("update-preview", plan.app_id))
        return plan

    def update(
        self,
        request: AppUpdateRequest,
        *,
        expected_plan: AppUpdatePlan | None = None,
    ) -> AppUpdateResult:
        plan = self.preview(request)
        if expected_plan is not None and plan != expected_plan:
            raise AppUpdatePlanChangedError("application update plan changed")
        self.events.append(("update", plan.app_id))
        if request.repository in self.fail_update:
            raise AppUpdateError("private update failure")
        self.publisher.publish(request.repository, plan.changes, lambda: None)
        return AppUpdateResult(plan, False)


class _RecordingPublisher:
    def __init__(
        self,
        events: list[tuple],
        *,
        fail_calls: set[int] | None = None,
    ) -> None:
        self.events = events
        self.fail_calls = fail_calls or set()
        self.calls: list[tuple[FileChange, ...]] = []

    def publish(self, repository, changes, validate) -> None:
        self.calls.append(changes)
        call = len(self.calls)
        self.events.append(("publish", repository.name, call))
        if call in self.fail_calls:
            raise ValueError("private publication failure")
        before: dict[Path, bytes | None] = {}
        for change in changes:
            target = repository / change.path
            content = target.read_bytes() if target.is_file() else None
            if content != change.before:
                raise ValueError("unexpected source bytes")
            before[change.path] = content
        try:
            for change in changes:
                target = repository / change.path
                if change.after is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(change.after)
            validate()
        except BaseException:
            for path, content in before.items():
                target = repository / path
                if content is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            raise


class _TransactionCommitter:
    def __init__(
        self,
        events: list[tuple],
        *,
        fail_update_calls: set[int] | None = None,
        fail_restoration_calls: set[int] | None = None,
    ) -> None:
        self.delegate = AppPlatformCommitter()
        self.events = events
        self.fail_update_calls = fail_update_calls or set()
        self.fail_restoration_calls = fail_restoration_calls or set()
        self.update_calls = 0
        self.restoration_calls = 0

    def inspect_clean_main(self, repository: Path) -> AppGitState:
        state = self.delegate.inspect_clean_main(repository)
        self.events.append(("inspect", repository.name, state.head))
        return state

    def commit_update(self, repository, plan, expected_head) -> str:
        self.update_calls += 1
        self.events.append(("commit-update", plan.app_id))
        if self.update_calls in self.fail_update_calls:
            raise AppPlatformCommitError("private update commit failure")
        return self.delegate.commit_update(repository, plan, expected_head)

    def commit_restoration(self, repository, plan, expected_head) -> str:
        self.restoration_calls += 1
        self.events.append(("commit-restoration", plan.app_id))
        if self.restoration_calls in self.fail_restoration_calls:
            raise AppPlatformCommitError("private restoration failure")
        return self.delegate.commit_restoration(repository, plan, expected_head)


class _Checker:
    def __init__(
        self, events: list[tuple], *, fail_calls: set[int] | None = None
    ) -> None:
        self.events = events
        self.fail_calls = fail_calls or set()
        self.calls = 0

    def check(self, repository: Path) -> object:
        self.calls += 1
        self.events.append(("check", repository.name, self.calls))
        if self.calls in self.fail_calls:
            raise ValueError("private check failure")
        return object()


class _Activator:
    def __init__(
        self,
        events: list[tuple],
        live_commits: dict[Path, str],
        *,
        fail_preview_calls: set[int] | None = None,
        fail_activate_calls: set[int] | None = None,
        unverified_calls: set[int] | None = None,
        wrong_commit_calls: set[int] | None = None,
        wrong_app_id_calls: set[int] | None = None,
        preview_overrides: dict[int, tuple[str, str, str, int | None, bool]] | None = None,
        before_activations: dict[int, object] | None = None,
        after_activations: dict[int, object] | None = None,
        planning_calls: int = 0,
    ) -> None:
        self.events = events
        self.live_commits = live_commits
        self.fail_preview_calls = fail_preview_calls or set()
        self.fail_activate_calls = fail_activate_calls or set()
        self.unverified_calls = unverified_calls or set()
        self.wrong_commit_calls = wrong_commit_calls or set()
        self.wrong_app_id_calls = wrong_app_id_calls or set()
        self.preview_overrides = preview_overrides or {}
        self.before_activations = before_activations or {}
        self.after_activations = after_activations or {}
        self.planning_calls = planning_calls
        self.preview_calls = 0
        self.activate_calls = 0

    def preview(self, repository: Path) -> AppActivationPlan:
        if self.planning_calls:
            self.planning_calls -= 1
            manifest = json.loads((repository / "local-web.json").read_text())
            return AppActivationPlan(
                manifest["id"], manifest["route"], manifest["kind"], None, False
            )
        self.preview_calls += 1
        self.events.append(("activation-preview", repository.name, self.preview_calls))
        if self.preview_calls in self.fail_preview_calls:
            raise ValueError("private activation preview failure")
        manifest = json.loads((repository / "local-web.json").read_text())
        identity = self.preview_overrides.get(
            self.preview_calls,
            (manifest["id"], manifest["route"], manifest["kind"], None, False),
        )
        if len(identity) == 3:
            identity = (*identity, None, False)
        return AppActivationPlan(
            identity[0], identity[1], identity[2], identity[3], identity[4]
        )

    def activate(
        self,
        repository: Path,
        *,
        expected_plan: AppActivationPlan | None = None,
        expected_source_commit: str | None = None,
        expected_former_live_commit: str | None = None,
    ) -> AppActivationResult:
        self.activate_calls += 1
        self.events.append(("activate", repository.name, self.activate_calls))
        if self.activate_calls in self.fail_activate_calls:
            raise ValueError("private activation failure")
        plan = self.preview(repository)
        if expected_plan is not None and plan != expected_plan:
            raise ValueError("private activation plan race")
        callback = self.before_activations.get(self.activate_calls)
        if callback is not None:
            callback(repository)
        if (
            expected_former_live_commit is not None
            and self.live_commits.get(repository) != expected_former_live_commit
        ):
            raise ValueError("private former live race")
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if expected_source_commit is not None and commit != expected_source_commit:
            raise ValueError("private source commit race")
        verified = self.activate_calls not in self.unverified_calls
        deployment_commit = (
            "e" * 40
            if self.activate_calls in self.wrong_commit_calls
            else (commit if verified else "f" * 40)
        )
        if verified:
            self.live_commits[repository] = commit
        result = AppActivationResult(
            plan,
            None,
            DeploymentResult(
                (
                    "private-wrong-app"
                    if self.activate_calls in self.wrong_app_id_calls
                    else plan.app_id
                ),
                deployment_commit,
                "deployed",
                "deployed",
                None,
            ),
            verified,
        )
        callback = self.after_activations.get(self.activate_calls)
        if callback is not None:
            callback(repository)
        return result


class FleetUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.sentinel = self.runtime / "sentinel"
        self.sentinel.write_bytes(b"runtime remains untouched\n")
        self.repositories = {
            app_id: self.make_repository(app_id, platform=platform)
            for app_id, platform in (
                ("current-app", True),
                ("ready-app", True),
                ("legacy-app", False),
                ("foundation-app", True),
                ("dirty-app", True),
                ("non-main-app", True),
                ("invalid-app", True),
                ("failing-app", True),
            )
        }
        outer_repository = self.make_repository("outer-app", platform=True)
        nested_repository = outer_repository / "nested"
        nested_repository.mkdir()
        self.repositories["nested-app"] = nested_repository
        (self.repositories["dirty-app"] / "private-note.txt").write_bytes(
            b"dirty\n"
        )
        self.git(self.repositories["non-main-app"], "switch", "-c", "feature")
        (self.repositories["invalid-app"] / "local-web.json").write_bytes(
            b"{invalid\n"
        )
        self.git(self.repositories["invalid-app"], "add", "local-web.json")
        self.git(self.repositories["invalid-app"], "commit", "-m", "malformed manifest")
        self.registry_path = self.write_registry(
            (
                "current-app",
                "ready-app",
                "legacy-app",
                "foundation-app",
                "dirty-app",
                "non-main-app",
                "unavailable-app",
                "nested-app",
                "invalid-app",
                "failing-app",
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def make_repository(self, app_id: str, *, platform: bool) -> Path:
        repository = self.root / app_id
        repository.mkdir()
        manifest = {
            "schemaVersion": 1,
            "id": app_id,
            "title": app_id,
            "route": f"/{app_id}",
            "kind": "static",
            "build": {
                "commands": [["npm", "run", "build"]],
                "output": "dist",
                "environment": [],
            },
            "healthPath": f"/{app_id}/",
        }
        if platform:
            manifest["platform"] = {
                "contractVersion": 1,
                "templateVersion": 2,
                "uiVersion": "1.0.0",
                "capabilities": [],
            }
            provenance = AppProvenance(
                schema_version=1,
                template_version=2,
                platform_contract_version=1,
                ui=UiArtifactReference("1.0.0", "a" * 64),
                capabilities=(),
                domain_palette_tokens=(),
                managed_files=(Path("local-web.json"),),
            )
            (repository / ".local-web-platform.json").write_bytes(
                render_provenance(provenance)
            )
        (repository / "local-web.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (repository / "tracked.txt").write_bytes(b"base\n")
        self.git(repository, "init", "--initial-branch=main")
        self.git(repository, "add", "--all")
        self.git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "baseline",
        )
        return repository.resolve()

    def write_registry(self, app_ids: tuple[str, ...]) -> Path:
        apps = []
        for app_id in app_ids:
            repository = self.repositories.get(app_id, self.root / "unavailable-app")
            apps.append({"id": app_id, "repository": str(repository), "autoDeploy": True})
        registry_path = self.root / "apps.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "fleet.test",
                    "runtimeRoot": str(self.runtime),
                    "apps": apps,
                }
            ),
            encoding="utf-8",
        )
        return registry_path

    def update_plan(
        self,
        app_id: str,
        mode: str,
        *,
        changes: tuple[FileChange, ...] = (),
        foundation: str | None = None,
    ) -> AppUpdatePlan:
        return AppUpdatePlan(
            app_id=app_id,
            mode=mode,
            current_ui_version="1.0.0",
            target_ui_version="2.0.0",
            target_ui_sha256="b" * 64,
            capabilities=(),
            changes=changes,
            foundation=foundation,
        )

    def test_preview_classifies_registered_apps_in_order_without_writing(self):
        ready_change = FileChange(
            Path("vendor/local-web-ui.tgz"), b"old\n", b"new\n"
        )
        updater = _Updater(
            {
                self.repositories["current-app"]: self.update_plan(
                    "current-app", "current"
                ),
                self.repositories["ready-app"]: self.update_plan(
                    "ready-app", "refresh", changes=(ready_change,)
                ),
                self.repositories["foundation-app"]: self.update_plan(
                    "foundation-app", "adopt", foundation="react-vite"
                ),
            },
            {self.repositories["failing-app"]},
        )
        committer = _Committer(
            {
                repository: self.git(repository, "rev-parse", "HEAD").strip()
                for repository in self.repositories.values()
                if repository not in {
                    self.repositories["dirty-app"],
                    self.repositories["non-main-app"],
                    self.repositories["nested-app"],
                }
            },
            {
                self.repositories["dirty-app"]: "repository-not-clean",
                self.repositories["non-main-app"]: "repository-not-main",
                self.root / "unavailable-app": "repository-unavailable",
                self.repositories["nested-app"]: "repository-unavailable",
            },
        )
        before_registry = self.registry_path.read_bytes()
        before_tree = _tree_snapshot(self.root)
        before_status = {
            app_id: self.git(
                repository, "status", "--porcelain=v1", "--ignored=matching"
            )
            for app_id, repository in self.repositories.items()
        }
        before_sentinel = self.sentinel.read_bytes()

        plan = FleetUpdater(
            updater_factory=lambda: updater,
            activator_factory=lambda _registry: _PlanningActivator(),
            committer=committer,
            live_inspector=_LiveInspector(
                self.repositories,
                {
                    repository: self.git(repository, "rev-parse", "HEAD").strip()
                    for repository in self.repositories.values()
                },
            ),
        ).preview(self.registry_path)

        self.assertEqual(
            [(item.app_id, item.status, item.reason) for item in plan.apps],
            [
                ("current-app", "CURRENT", None),
                ("ready-app", "READY", None),
                ("legacy-app", "SKIPPED", "legacy-adoption-required"),
                ("foundation-app", "SKIPPED", "foundation-adoption-required"),
                ("dirty-app", "BLOCKED", "repository-not-clean"),
                ("non-main-app", "BLOCKED", "repository-not-main"),
                ("unavailable-app", "BLOCKED", "repository-unavailable"),
                ("nested-app", "BLOCKED", "repository-unavailable"),
                ("invalid-app", "BLOCKED", "application-contract-invalid"),
                ("failing-app", "BLOCKED", "update-planning-failed"),
            ],
        )
        self.assertEqual(
            updater.requests,
            [
                AppUpdateRequest(self.repositories["current-app"]),
                AppUpdateRequest(self.repositories["ready-app"]),
                AppUpdateRequest(self.repositories["foundation-app"]),
                AppUpdateRequest(self.repositories["failing-app"]),
            ],
        )
        self.assertFalse(updater.update_called)
        self.assertFalse(committer.commit_called)
        self.assertEqual(
            committer.inspected,
            [
                self.repositories["current-app"],
                self.repositories["ready-app"],
                self.repositories["legacy-app"],
                self.repositories["foundation-app"],
                self.repositories["dirty-app"],
                self.repositories["non-main-app"],
                self.root / "unavailable-app",
                self.repositories["nested-app"],
                self.repositories["invalid-app"],
                self.repositories["failing-app"],
            ],
        )
        self.assertEqual(
            plan.apps[0].source_head,
            self.git(self.repositories["current-app"], "rev-parse", "HEAD").strip(),
        )
        self.assertEqual(plan.apps[1].paths, (Path("vendor/local-web-ui.tgz"),))
        self.assertEqual(self.registry_path.read_bytes(), before_registry)
        self.assertEqual(_tree_snapshot(self.root), before_tree)
        self.assertEqual(
            {
                app_id: self.git(
                    repository, "status", "--porcelain=v1", "--ignored=matching"
                )
                for app_id, repository in self.repositories.items()
            },
            before_status,
        )
        self.assertEqual(self.sentinel.read_bytes(), before_sentinel)

    def test_preview_skips_a_plan_that_requires_foundation_adoption(self):
        repository = self.repositories["foundation-app"]
        updater = _Updater(
            {
                repository: self.update_plan(
                    "foundation-app",
                    "refresh",
                    changes=(FileChange(Path("package.json"), b"old\n", b"new\n"),),
                    foundation="react-vite",
                ),
            },
            set(),
        )
        committer = _Committer(
            {repository: self.git(repository, "rev-parse", "HEAD").strip()}
        )
        registry_path = self.write_registry(("foundation-app",))

        plan = FleetUpdater(
            updater_factory=lambda: updater,
            activator_factory=lambda _registry: _PlanningActivator(),
            committer=committer,
            live_inspector=_LiveInspector(
                self.repositories,
                {repository: self.git(repository, "rev-parse", "HEAD").strip()},
            ),
        ).preview(registry_path)

        self.assertEqual(
            [(item.status, item.reason) for item in plan.apps],
            [("SKIPPED", "foundation-adoption-required")],
        )
        self.assertFalse(committer.commit_called)

    def test_preview_uses_the_inspector_outcome_without_its_own_git_commands(self):
        repository = self.repositories["current-app"]
        registry_path = self.write_registry(("current-app",))

        with patch("subprocess.run") as run:
            plan = FleetUpdater(
                updater_factory=lambda: _Updater({}, set()),
                activator_factory=lambda _registry: _PlanningActivator(),
                committer=_InspectionFailureCommitter("repository-not-main"),
            ).preview(registry_path)

        self.assertEqual(
            [(item.status, item.reason) for item in plan.apps],
            [("BLOCKED", "repository-not-main")],
        )
        run.assert_not_called()

    def make_refresh_plan(self, app_id: str) -> AppUpdatePlan:
        return self.update_plan(
            app_id,
            "refresh",
            changes=(
                FileChange(Path("tracked.txt"), b"base\n", b"updated\n"),
                FileChange(Path("generated.txt"), None, b"generated\n"),
            ),
        )

    def make_apply_fleet(
        self,
        app_ids: tuple[str, ...],
        plans: dict[Path, tuple[AppUpdatePlan, ...]],
        *,
        updater_failures: set[Path] | None = None,
        preview_callbacks: dict[tuple[Path, int], object] | None = None,
        publisher_failures: set[int] | None = None,
        commit_update_failures: set[int] | None = None,
        commit_restoration_failures: set[int] | None = None,
        check_failures: set[int] | None = None,
        activation_preview_failures: set[int] | None = None,
        activation_failures: set[int] | None = None,
        unverified_activations: set[int] | None = None,
        wrong_commit_activations: set[int] | None = None,
        wrong_app_id_activations: set[int] | None = None,
        activation_preview_overrides: dict[
            int, tuple[str, str, str, int | None, bool]
        ] | None = None,
        updater_publisher=None,
        before_activations: dict[int, object] | None = None,
        after_activations: dict[int, object] | None = None,
    ):
        events: list[tuple] = []
        publisher = _RecordingPublisher(events, fail_calls=publisher_failures)
        updater = _ApplyingUpdater(
            plans,
            updater_publisher or publisher,
            events,
            fail_update=updater_failures,
            before_preview=preview_callbacks,
        )
        committer = _TransactionCommitter(
            events,
            fail_update_calls=commit_update_failures,
            fail_restoration_calls=commit_restoration_failures,
        )
        checker = _Checker(events, fail_calls=check_failures)
        live_commits = {
            repository: self.git(repository, "rev-parse", "HEAD").strip()
            for repository in plans
        }
        activator = _Activator(
            events,
            live_commits,
            fail_preview_calls=activation_preview_failures,
            fail_activate_calls=activation_failures,
            unverified_calls=unverified_activations,
            wrong_commit_calls=wrong_commit_activations,
            wrong_app_id_calls=wrong_app_id_activations,
            preview_overrides=activation_preview_overrides,
            before_activations=before_activations,
            after_activations=after_activations,
            planning_calls=len(plans),
        )
        fleet = FleetUpdater(
            updater_factory=lambda: updater,
            checker_factory=lambda: checker,
            activator_factory=lambda _registry_path: activator,
            committer=committer,
            publisher=publisher,
            live_inspector=_LiveInspector(self.repositories, live_commits),
        )
        return (
            fleet,
            self.write_registry(app_ids),
            events,
            publisher,
            committer,
            checker,
            activator,
            live_commits,
        )

    def test_preview_skips_pending_registry_transition_before_app_mutation(self):
        repository = self.repositories["ready-app"]
        updater = _Updater(
            {repository: self.make_refresh_plan("ready-app")}, set()
        )
        committer = _Committer(
            {repository: self.git(repository, "rev-parse", "HEAD").strip()}
        )
        activator = _PlanningActivator({repository})
        registry = self.write_registry(("ready-app",))

        plan = FleetUpdater(
            updater_factory=lambda: updater,
            activator_factory=lambda _registry: activator,
            committer=committer,
            live_inspector=_LiveInspector(
                self.repositories,
                {repository: self.git(repository, "rev-parse", "HEAD").strip()},
            ),
        ).preview(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in plan.apps],
            [("SKIPPED", "activation-transition-required")],
        )
        self.assertEqual(activator.calls, [repository])
        self.assertFalse(updater.update_called)

    def test_apply_blocks_a_late_registration_transition_before_publication(self):
        app_id = "late-registration-transition"
        later_id = "late-registration-later"
        repository = self.make_repository(app_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[app_id] = repository
        self.repositories[later_id] = later
        fleet, registry, events, publisher, committer, checker, activator, _ = (
            self.make_apply_fleet(
                (app_id, later_id),
                {
                    repository: (self.make_refresh_plan(app_id),),
                    later: (self.make_refresh_plan(later_id),),
                },
                activation_preview_overrides={
                    1: (
                        app_id,
                        f"/{app_id}",
                        "static",
                        None,
                        True,
                    )
                },
            )
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [("BLOCKED", "plan-changed"), ("UPDATED", None)],
        )
        self.assertNotIn(("update", app_id), events)
        self.assertIn(("update", later_id), events)
        self.assertEqual(publisher.calls, [
            self.make_refresh_plan(later_id).changes
        ])
        self.assertEqual(committer.update_calls, 1)
        self.assertEqual(checker.calls, 1)
        self.assertEqual(activator.activate_calls, 1)

    def test_runtime_live_inspector_reads_only_valid_release_pointer_state(self):
        runtime = self.root / "live-runtime"
        layout = RuntimeLayout(runtime, "live-app")
        commit = "a" * 40
        release = layout.releases / commit
        release.mkdir(parents=True)
        layout.current.symlink_to(Path("releases") / commit)
        inspector = RuntimeLiveReleaseInspector()

        self.assertEqual(inspector.inspect(runtime, "live-app"), commit)
        self.assertIsNone(inspector.inspect(runtime, "missing-app"))

        layout.current.unlink()
        layout.current.write_text("private malformed pointer")
        with self.assertRaisesRegex(
            LiveReleaseInspectionError, "live release inspection failed"
        ) as raised:
            inspector.inspect(runtime, "live-app")
        self.assertNotIn(str(runtime), str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_preview_blocks_current_source_with_stale_missing_or_malformed_live_release(self):
        for case, value in (
            ("stale", "f" * 40),
            ("missing", None),
            ("malformed", LiveReleaseInspectionError("private live detail")),
        ):
            with self.subTest(case=case):
                app_id = f"current-live-{case}"
                repository = self.make_repository(app_id, platform=True)
                self.repositories[app_id] = repository
                updater = _Updater(
                    {repository: self.update_plan(app_id, "current")}, set()
                )
                committer = _Committer(
                    {repository: self.git(repository, "rev-parse", "HEAD").strip()}
                )
                plan = FleetUpdater(
                    updater_factory=lambda: updater,
                    activator_factory=lambda _registry: _PlanningActivator(),
                    committer=committer,
                    live_inspector=_LiveInspector(
                        self.repositories, {repository: value}
                    ),
                ).preview(self.write_registry((app_id,)))

                self.assertEqual(
                    [(item.status, item.reason) for item in plan.apps],
                    [("BLOCKED", "live-release-not-current")],
                )
                self.assertNotIn("private", plan.apps[0].reason or "")

    def test_current_and_ready_plans_carry_source_and_live_commits_separately(self):
        current = self.repositories["current-app"]
        ready = self.repositories["ready-app"]
        current_head = self.git(current, "rev-parse", "HEAD").strip()
        ready_head = self.git(ready, "rev-parse", "HEAD").strip()
        updater = _Updater(
            {
                current: self.update_plan("current-app", "current"),
                ready: self.make_refresh_plan("ready-app"),
            },
            set(),
        )
        plan = FleetUpdater(
            updater_factory=lambda: updater,
            activator_factory=lambda _registry: _PlanningActivator(),
            committer=_Committer({current: current_head, ready: ready_head}),
            live_inspector=_LiveInspector(
                self.repositories, {current: current_head, ready: ready_head}
            ),
        ).preview(self.write_registry(("current-app", "ready-app")))

        self.assertEqual(
            [(item.source_head, item.deployed_commit) for item in plan.apps],
            [(current_head, current_head), (ready_head, ready_head)],
        )

    def test_apply_blocks_live_release_race_before_publication_and_continues(self):
        app_id = "live-race"
        later_id = "live-race-later"
        repository = self.make_repository(app_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[app_id] = repository
        self.repositories[later_id] = later
        fleet, registry, events, publisher, *_ = self.make_apply_fleet(
            (app_id, later_id),
            {
                repository: (self.make_refresh_plan(app_id),),
                later: (self.make_refresh_plan(later_id),),
            },
        )
        original_inspector = fleet._live_inspector
        calls: dict[str, int] = {}

        class RacingInspector:
            def inspect(inner_self, runtime_root: Path, inspected_id: str):
                calls[inspected_id] = calls.get(inspected_id, 0) + 1
                value = original_inspector.inspect(runtime_root, inspected_id)
                if inspected_id == app_id and calls[inspected_id] == 2:
                    return "e" * 40
                return value

        fleet._live_inspector = RacingInspector()

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [("BLOCKED", "live-release-changed"), ("UPDATED", None)],
        )
        self.assertNotIn(("update", app_id), events)
        self.assertEqual(len(publisher.calls), 1)

    def test_activation_apply_plan_race_and_deployment_app_id_are_rejected(self):
        for case in ("port-race", "registry-race", "deployment-app-id"):
            with self.subTest(case=case):
                app_id = f"activation-binding-{case}"
                later_id = f"activation-binding-later-{case}"
                repository = self.make_repository(app_id, platform=True)
                later = self.make_repository(later_id, platform=True)
                self.repositories[app_id] = repository
                self.repositories[later_id] = later
                overrides = None
                wrong_ids = None
                if case == "port-race":
                    overrides = {3: (app_id, f"/{app_id}", "static", 52000, False)}
                elif case == "registry-race":
                    overrides = {3: (app_id, f"/{app_id}", "static", None, True)}
                else:
                    wrong_ids = {1}
                fleet, registry, events, *_ = self.make_apply_fleet(
                    (app_id, later_id),
                    {
                        repository: (self.make_refresh_plan(app_id),),
                        later: (self.make_refresh_plan(later_id),),
                    },
                    activation_preview_overrides=overrides,
                    wrong_app_id_activations=wrong_ids,
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [("FAILED_RECOVERED", "activation-failed" if case != "deployment-app-id" else "verification-failed"), ("UPDATED", None)],
                )
                self.assertIn(("update", later_id), events)

    def test_apply_updates_only_ready_apps_in_order_and_verifies_exact_commits(self):
        later = self.make_repository("later-ready-app", platform=True)
        self.repositories["later-ready-app"] = later
        ready = self.repositories["ready-app"]
        current = self.repositories["current-app"]
        plans = {
            current: (self.update_plan("current-app", "current"),),
            ready: (self.make_refresh_plan("ready-app"),),
            later: (self.make_refresh_plan("later-ready-app"),),
        }
        fleet, registry, events, publisher, committer, checker, _, live = (
            self.make_apply_fleet(
                (
                    "current-app",
                    "ready-app",
                    "legacy-app",
                    "dirty-app",
                    "later-ready-app",
                ),
                plans,
            )
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [item.status for item in result.apps],
            ["CURRENT", "UPDATED", "SKIPPED", "BLOCKED", "UPDATED"],
        )
        self.assertTrue(result.apps[0].verified)
        self.assertEqual(
            result.apps[0].source_head, result.apps[0].deployed_commit
        )
        self.assertEqual(result.apps[1].source_head, live[ready])
        self.assertEqual(result.apps[1].deployed_commit, live[ready])
        self.assertTrue(result.apps[1].verified)
        self.assertEqual(result.apps[4].source_head, live[later])
        self.assertEqual(result.apps[4].deployed_commit, live[later])
        self.assertEqual(
            [event[1] for event in events if event[0] == "update"],
            ["ready-app", "later-ready-app"],
        )
        self.assertEqual(
            [event[1] for event in events if event[0] == "commit-update"],
            ["ready-app", "later-ready-app"],
        )
        self.assertEqual(
            [event[1] for event in events if event[0] == "check"],
            ["ready-app", "later-ready-app"],
        )
        self.assertEqual(len(publisher.calls), 2)
        self.assertEqual(committer.restoration_calls, 0)
        self.assertEqual(self.git(ready, "status", "--porcelain=v1"), "")
        self.assertEqual(self.git(later, "status", "--porcelain=v1"), "")
        self.assertEqual((ready / "tracked.txt").read_bytes(), b"updated\n")
        self.assertEqual((later / "generated.txt").read_bytes(), b"generated\n")

    def test_apply_blocks_every_changed_plan_or_git_state_and_continues(self):
        cases = ("plan", "head", "branch", "worktree", "digest", "paths")
        for case in cases:
            with self.subTest(case=case):
                race_id = f"race-{case}"
                later_id = f"later-{case}"
                race = self.make_repository(race_id, platform=True)
                later = self.make_repository(later_id, platform=True)
                self.repositories[race_id] = race
                self.repositories[later_id] = later
                original = self.make_refresh_plan(race_id)
                changed = original
                callback = None
                if case == "plan":
                    changed = self.update_plan(
                        race_id,
                        "refresh",
                        changes=original.changes,
                    )
                    changed = AppUpdatePlan(
                        **{**changed.__dict__, "current_ui_version": "0.9.0"}
                    )
                elif case == "digest":
                    changed = AppUpdatePlan(
                        **{**original.__dict__, "target_ui_sha256": "c" * 64}
                    )
                elif case == "paths":
                    changed = AppUpdatePlan(
                        **{
                            **original.__dict__,
                            "changes": (
                                FileChange(
                                    Path("different.txt"), None, b"different\n"
                                ),
                            ),
                        }
                    )
                elif case == "head":
                    def change_head(repository=race):
                        (repository / "race.txt").write_bytes(b"race\n")
                        self.git(repository, "add", "race.txt")
                        self.git(repository, "commit", "-m", "race")

                    callback = change_head
                elif case == "branch":
                    callback = lambda repository=race: self.git(
                        repository, "switch", "-c", "feature"
                    )
                elif case == "worktree":
                    callback = lambda repository=race: (
                        repository / "race.txt"
                    ).write_bytes(b"race\n")
                callbacks = (
                    {(race, 2): callback} if callback is not None else None
                )
                fleet, registry, events, *_ = self.make_apply_fleet(
                    (race_id, later_id),
                    {
                        race: (original, changed),
                        later: (self.make_refresh_plan(later_id),),
                    },
                    preview_callbacks=callbacks,
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [("BLOCKED", "plan-changed"), ("UPDATED", None)],
                )
                self.assertNotIn(("update", race_id), events)
                self.assertIn(("update", later_id), events)

    def test_apply_blocks_third_update_plan_drift_before_mutation_and_continues(self):
        race_id = "race-third-plan"
        later_id = "later-third-plan"
        race = self.make_repository(race_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[race_id] = race
        self.repositories[later_id] = later
        original = self.make_refresh_plan(race_id)
        changed = AppUpdatePlan(
            **{**original.__dict__, "target_ui_sha256": "c" * 64}
        )
        fleet, registry, events, publisher, committer, checker, activator, _ = (
            self.make_apply_fleet(
                (race_id, later_id),
                {
                    race: (original, original, changed),
                    later: (self.make_refresh_plan(later_id),),
                },
            )
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [("BLOCKED", "plan-changed"), ("UPDATED", None)],
        )
        self.assertNotIn(("update", race_id), events)
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(committer.update_calls, 1)
        self.assertEqual(checker.calls, 1)
        self.assertEqual(activator.activate_calls, 1)
        self.assertEqual((race / "tracked.txt").read_bytes(), b"base\n")
        self.assertFalse((race / "generated.txt").exists())

    def test_each_apply_failure_recovers_exactly_and_later_app_continues(self):
        stages = (
            "update",
            "commit",
            "check",
            "activation-preview",
            "activation",
            "verification",
            "deployment-commit",
        )
        expected_reasons = {
            "update": "update-failed",
            "commit": "commit-failed",
            "check": "check-failed",
            "activation-preview": "activation-preview-failed",
            "activation": "activation-failed",
            "verification": "verification-failed",
            "deployment-commit": "verification-failed",
        }
        for stage in stages:
            with self.subTest(stage=stage):
                failed_id = f"failed-{stage}"
                later_id = f"continued-{stage}"
                failed = self.make_repository(failed_id, platform=True)
                later = self.make_repository(later_id, platform=True)
                self.repositories[failed_id] = failed
                self.repositories[later_id] = later
                original_head = self.git(failed, "rev-parse", "HEAD").strip()
                original_live = original_head
                plan = self.make_refresh_plan(failed_id)
                options = {
                    "updater_failures": {failed} if stage == "update" else None,
                    "commit_update_failures": {1} if stage == "commit" else None,
                    "check_failures": {1} if stage == "check" else None,
                    "activation_preview_failures": (
                        {2} if stage == "activation-preview" else None
                    ),
                    "activation_failures": {1} if stage == "activation" else None,
                    "unverified_activations": (
                        {1} if stage == "verification" else None
                    ),
                    "wrong_commit_activations": (
                        {1} if stage == "deployment-commit" else None
                    ),
                }
                fleet, registry, events, publisher, committer, _, _, live = (
                    self.make_apply_fleet(
                        (failed_id, later_id),
                        {
                            failed: (plan,),
                            later: (self.make_refresh_plan(later_id),),
                        },
                        **options,
                    )
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [
                        ("FAILED_RECOVERED", expected_reasons[stage]),
                        ("UPDATED", None),
                    ],
                )
                self.assertTrue(result.apps[0].verified)
                self.assertEqual((failed / "tracked.txt").read_bytes(), b"base\n")
                self.assertFalse((failed / "generated.txt").exists())
                self.assertEqual(self.git(failed, "status", "--porcelain=v1"), "")
                self.assertIn(("update", later_id), events)
                if stage == "update":
                    self.assertEqual(
                        self.git(failed, "rev-parse", "HEAD").strip(), original_head
                    )
                    self.assertEqual(live[failed], original_live)
                    self.assertEqual(result.apps[0].source_head, original_head)
                    self.assertEqual(result.apps[0].deployed_commit, original_live)
                    self.assertEqual(len(publisher.calls), 1)
                elif stage == "commit":
                    self.assertEqual(
                        self.git(failed, "rev-parse", "HEAD").strip(), original_head
                    )
                    self.assertEqual(live[failed], original_live)
                    self.assertEqual(result.apps[0].source_head, original_head)
                    self.assertEqual(result.apps[0].deployed_commit, original_live)
                    self.assertEqual(publisher.calls[1], tuple(
                        FileChange(change.path, change.after, change.before)
                        for change in plan.changes
                    ))
                    self.assertEqual(committer.restoration_calls, 0)
                else:
                    self.assertEqual(publisher.calls[1], tuple(
                        FileChange(change.path, change.after, change.before)
                        for change in plan.changes
                    ))
                    self.assertEqual(committer.restoration_calls, 1)
                    self.assertEqual(
                        live[failed],
                        self.git(failed, "rev-parse", "HEAD").strip(),
                    )
                    self.assertEqual(
                        result.apps[0].source_head,
                        self.git(failed, "rev-parse", "HEAD").strip(),
                    )
                    self.assertEqual(
                        result.apps[0].deployed_commit,
                        result.apps[0].source_head,
                    )

    def test_any_failed_compensation_stops_before_the_following_app(self):
        stages = (
            "inverse",
            "restoration-commit",
            "recovery-check",
            "recovery-preview",
            "rollback-activation",
            "rollback-verification",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                failed_id = f"recovery-{stage}"
                later_id = f"stopped-{stage}"
                failed = self.make_repository(failed_id, platform=True)
                later = self.make_repository(later_id, platform=True)
                self.repositories[failed_id] = failed
                self.repositories[later_id] = later
                options = {
                    "check_failures": (
                        {1, 2} if stage == "recovery-check" else {1}
                    ),
                    "publisher_failures": {2} if stage == "inverse" else None,
                    "commit_restoration_failures": (
                        {1} if stage == "restoration-commit" else None
                    ),
                    "activation_failures": (
                        {1} if stage == "rollback-activation" else None
                    ),
                    "activation_preview_failures": (
                        {2} if stage == "recovery-preview" else None
                    ),
                    "unverified_activations": (
                        {1} if stage == "rollback-verification" else None
                    ),
                }
                fleet, registry, events, *_ = self.make_apply_fleet(
                    (failed_id, later_id),
                    {
                        failed: (self.make_refresh_plan(failed_id),),
                        later: (self.make_refresh_plan(later_id),),
                    },
                    **options,
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [
                        ("RECOVERY_FAILED", "recovery-failed"),
                        ("BLOCKED", "recovery-failed"),
                    ],
                )
                self.assertNotIn(("update", later_id), events)

    def test_activation_preview_identity_mismatch_recovers_without_exposing_detail(self):
        mismatches = {
            "id": ("different-app", "/ready-app", "static"),
            "route": ("ready-app", "/different", "static"),
            "kind": ("ready-app", "/ready-app", "service"),
        }
        for field, identity in mismatches.items():
            with self.subTest(field=field):
                app_id = f"identity-{field}"
                repository = self.make_repository(app_id, platform=True)
                self.repositories[app_id] = repository
                expected_identity = (
                    identity[0] if field == "id" else app_id,
                    identity[1] if field == "route" else f"/{app_id}",
                    identity[2],
                )
                fleet, registry, *_ = self.make_apply_fleet(
                    (app_id,),
                    {repository: (self.make_refresh_plan(app_id),)},
                    activation_preview_overrides={2: expected_identity},
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [("FAILED_RECOVERED", "activation-preview-failed")],
                )
                self.assertNotIn("different", result.apps[0].reason or "")

    def test_activation_result_identity_mismatch_is_rejected_forward_and_during_recovery(self):
        for case in ("forward", "recovery"):
            for field in ("app-id", "route", "kind"):
                with self.subTest(case=case, field=field):
                    app_id = f"result-identity-{case}-{field}"
                    later_id = f"result-identity-later-{case}-{field}"
                    identity = {
                        "app-id": (
                            "private-different-app",
                            f"/{app_id}",
                            "static",
                        ),
                        "route": (app_id, "/private-different-route", "static"),
                        "kind": (app_id, f"/{app_id}", "service"),
                    }[field]
                    self._assert_activation_result_identity_mismatch(
                        case, app_id, later_id, identity
                    )

    def _assert_activation_result_identity_mismatch(
        self,
        case: str,
        app_id: str,
        later_id: str,
        identity: tuple[str, str, str],
    ) -> None:
        repository = self.make_repository(app_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[app_id] = repository
        self.repositories[later_id] = later
        fleet, registry, events, *_ = self.make_apply_fleet(
            (app_id, later_id),
            {
                repository: (self.make_refresh_plan(app_id),),
                later: (self.make_refresh_plan(later_id),),
            },
            check_failures={1} if case == "recovery" else None,
            activation_preview_overrides={3: identity},
        )

        result = fleet.apply(registry)

        if case == "forward":
            self.assertEqual(
                [(item.status, item.reason) for item in result.apps],
                [
                    ("FAILED_RECOVERED", "activation-failed"),
                    ("UPDATED", None),
                ],
            )
            self.assertIn(("update", later_id), events)
        else:
            self.assertEqual(
                [(item.status, item.reason) for item in result.apps],
                [
                    ("RECOVERY_FAILED", "recovery-failed"),
                    ("BLOCKED", "recovery-failed"),
                ],
            )
            self.assertNotIn(("update", later_id), events)
        self.assertNotIn("private", result.apps[0].reason or "")

    def test_mid_publication_failure_restores_exact_app_and_continues(self):
        class FailOnSecondReplace:
            def __init__(self) -> None:
                self.calls = 0
                self.first_mutation: tuple[Path, bytes] | None = None

            def __call__(self, source: Path, destination: Path) -> None:
                self.calls += 1
                if self.calls == 2:
                    raise OSError("private mid-publication failure")
                os.replace(source, destination)
                if self.calls == 1:
                    self.first_mutation = (destination, destination.read_bytes())

        failed_id = "publication-rollback"
        later_id = "publication-continued"
        failed = self.make_repository(failed_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[failed_id] = failed
        self.repositories[later_id] = later
        original_head = self.git(failed, "rev-parse", "HEAD").strip()
        fail_replace = FailOnSecondReplace()
        fleet, registry, events, _, _, _, _, live = self.make_apply_fleet(
            (failed_id, later_id),
            {
                failed: (self.make_refresh_plan(failed_id),),
                later: (self.make_refresh_plan(later_id),),
            },
            updater_publisher=UpdatePublisher(replace=fail_replace),
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [("FAILED_RECOVERED", "update-failed"), ("UPDATED", None)],
        )
        self.assertIsNotNone(fail_replace.first_mutation)
        self.assertEqual(fail_replace.first_mutation[1], b"updated\n")
        self.assertGreaterEqual(fail_replace.calls, 4)
        self.assertEqual((failed / "tracked.txt").read_bytes(), b"base\n")
        self.assertFalse((failed / "generated.txt").exists())
        self.assertEqual(self.git(failed, "status", "--porcelain=v1"), "")
        self.assertEqual(self.git(failed, "rev-parse", "HEAD").strip(), original_head)
        self.assertEqual(live[failed], original_head)
        self.assertIn(("update", later_id), events)

    def test_final_recovery_git_corruption_fails_closed_and_stops(self):
        failed_id = "recovery-final-git"
        later_id = "recovery-final-git-stopped"
        failed = self.make_repository(failed_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[failed_id] = failed
        self.repositories[later_id] = later

        def corrupt_after_verified_rollback(repository: Path) -> None:
            (repository / "post-rollback-race.txt").write_bytes(b"race\n")

        fleet, registry, events, publisher, committer, checker, _, live = (
            self.make_apply_fleet(
                (failed_id, later_id),
                {
                    failed: (self.make_refresh_plan(failed_id),),
                    later: (self.make_refresh_plan(later_id),),
                },
                check_failures={1},
                after_activations={1: corrupt_after_verified_rollback},
            )
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [
                ("RECOVERY_FAILED", "recovery-failed"),
                ("BLOCKED", "recovery-failed"),
            ],
        )
        self.assertEqual(len(publisher.calls), 2)
        self.assertEqual(committer.restoration_calls, 1)
        self.assertEqual(checker.calls, 2)
        self.assertIn(("activation-preview", failed_id, 1), events)
        self.assertIn(("activate", failed_id, 1), events)
        self.assertEqual(live[failed], self.git(failed, "rev-parse", "HEAD").strip())
        self.assertNotEqual(self.git(failed, "status", "--porcelain=v1"), "")
        self.assertNotIn(("update", later_id), events)

    def test_recovery_failure_reports_a_missing_live_release_as_missing(self):
        app_id = "recovery-live-missing"
        repository = self.make_repository(app_id, platform=True)
        self.repositories[app_id] = repository

        live: dict[Path, str] | None = None

        def remove_live_release(activated: Path) -> None:
            assert live is not None
            live.pop(activated)

        fleet, registry, _, _, _, _, _, live = self.make_apply_fleet(
            (app_id,),
            {repository: (self.make_refresh_plan(app_id),)},
            activation_failures={2},
            after_activations={1: remove_live_release},
        )

        result = fleet.apply(registry)

        self.assertEqual(result.apps[0].status, "RECOVERY_FAILED")
        self.assertIsNotNone(result.apps[0].source_head)
        self.assertIsNone(result.apps[0].deployed_commit)

    def test_external_live_selection_after_activation_is_not_overwritten(self):
        app_id = "late-external-live"
        later_id = "late-external-live-stopped"
        repository = self.make_repository(app_id, platform=True)
        later = self.make_repository(later_id, platform=True)
        self.repositories[app_id] = repository
        self.repositories[later_id] = later
        third_party_commit = "d" * 40
        live: dict[Path, str] | None = None

        def select_external_release(activated: Path) -> None:
            assert live is not None
            live[activated] = third_party_commit

        fleet, registry, events, _, committer, _, activator, live = (
            self.make_apply_fleet(
                (app_id, later_id),
                {
                    repository: (self.make_refresh_plan(app_id),),
                    later: (self.make_refresh_plan(later_id),),
                },
                after_activations={1: select_external_release},
            )
        )

        result = fleet.apply(registry)

        self.assertEqual(
            [(item.status, item.reason) for item in result.apps],
            [
                ("RECOVERY_FAILED", "recovery-failed"),
                ("BLOCKED", "recovery-failed"),
            ],
        )
        self.assertEqual(result.apps[0].deployed_commit, third_party_commit)
        self.assertEqual(live[repository], third_party_commit)
        self.assertEqual(committer.restoration_calls, 0)
        self.assertEqual(activator.activate_calls, 1)
        self.assertNotIn(("update", later_id), events)

    def test_live_selection_inside_forward_or_recovery_activation_is_not_overwritten(self):
        for stage in ("forward", "recovery"):
            with self.subTest(stage=stage):
                app_id = f"inside-live-{stage}"
                later_id = f"inside-live-{stage}-stopped"
                repository = self.make_repository(app_id, platform=True)
                later = self.make_repository(later_id, platform=True)
                self.repositories[app_id] = repository
                self.repositories[later_id] = later
                third_party_commit = "e" * 40
                live: dict[Path, str] | None = None

                def select_external_release(activated: Path) -> None:
                    assert live is not None
                    live[activated] = third_party_commit

                fleet, registry, events, _, _, _, activator, live = (
                    self.make_apply_fleet(
                        (app_id, later_id),
                        {
                            repository: (self.make_refresh_plan(app_id),),
                            later: (self.make_refresh_plan(later_id),),
                        },
                        check_failures={1} if stage == "recovery" else None,
                        before_activations={1: select_external_release},
                    )
                )

                result = fleet.apply(registry)

                self.assertEqual(
                    [(item.status, item.reason) for item in result.apps],
                    [
                        ("RECOVERY_FAILED", "recovery-failed"),
                        ("BLOCKED", "recovery-failed"),
                    ],
                )
                self.assertEqual(result.apps[0].deployed_commit, third_party_commit)
                self.assertEqual(live[repository], third_party_commit)
                self.assertEqual(activator.activate_calls, 1)
                self.assertNotIn(("update", later_id), events)


if __name__ == "__main__":
    unittest.main()
