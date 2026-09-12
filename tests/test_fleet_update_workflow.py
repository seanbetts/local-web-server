import contextlib
import json
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from tests.suites import acceptance


class FleetUpdateWorkflowTests(unittest.TestCase):
    def test_package_exposes_the_disposable_fleet_acceptance(self):
        import json

        package = json.loads(
            (Path(__file__).parents[1] / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            package["scripts"]["verify:fleet-update"],
            "python3 scripts/verify_fleet_update_workflow.py",
        )

    def test_workflow_has_exact_phases_and_cleans_every_injected_failure(self):
        import scripts.verify_fleet_update_workflow as verifier

        self.assertEqual(
            verifier.EXPECTED_PHASES,
            (
                "create disposable fleet PASS",
                "preview complete fleet PASS",
                "prove preview purity PASS",
                "apply compatible apps PASS",
                "verify focused app commits PASS",
                "verify exact live releases PASS",
                "classify unsupported app PASS",
                "recover injected app failure PASS",
                "prove aggregate result PASS",
                "cleanup PASS",
            ),
        )
        for phase in verifier.EXPECTED_PHASES:
            with self.subTest(phase=phase):
                roots: list[Path] = []
                lines: list[str] = []
                actions: list[str] = []

                class LightweightFleet:
                    def __init__(self, root: Path, *, require_cli_preview: bool):
                        self.root = root
                        self.require_cli_preview = require_cli_preview

                    def create(self):
                        actions.append(verifier.EXPECTED_PHASES[0])

                    def preview(self):
                        actions.append(verifier.EXPECTED_PHASES[1])

                    def assert_preview_pure(self):
                        actions.append(verifier.EXPECTED_PHASES[2])

                    def apply(self):
                        actions.append(verifier.EXPECTED_PHASES[3])

                    def verify_commits(self):
                        actions.append(verifier.EXPECTED_PHASES[4])

                    def verify_releases(self):
                        actions.append(verifier.EXPECTED_PHASES[5])

                    def verify_unsupported(self):
                        actions.append(verifier.EXPECTED_PHASES[6])

                    def verify_recovery(self):
                        actions.append(verifier.EXPECTED_PHASES[7])

                    def verify_aggregate(self):
                        actions.append(verifier.EXPECTED_PHASES[8])

                    def close(self):
                        actions.append(verifier.EXPECTED_PHASES[9])

                verifier_instance = verifier.FleetUpdateWorkflowVerifier(
                    temporary_directory_factory=verifier.RecordingTemporaryDirectory(roots),
                    fleet_factory=LightweightFleet,
                    fail_after=phase,
                    emit=lines.append,
                )
                self.assertEqual(verifier_instance.run(), 1)
                phase_index = verifier.EXPECTED_PHASES.index(phase)
                expected_actions = list(verifier.EXPECTED_PHASES[: phase_index + 1])
                if verifier.EXPECTED_PHASES[-1] not in expected_actions:
                    expected_actions.append(verifier.EXPECTED_PHASES[-1])
                self.assertEqual(actions, expected_actions)
                expected_lines = [
                    *verifier.EXPECTED_PHASES[:phase_index],
                    phase.replace(" PASS", " FAIL"),
                ]
                if phase != verifier.EXPECTED_PHASES[-1]:
                    expected_lines.append(verifier.EXPECTED_PHASES[-1])
                self.assertEqual(lines, expected_lines)
                self.assertEqual(len(roots), 1)
                self.assertFalse(roots[0].exists())
                self.assertNotIn(str(roots[0]), "\n".join(lines))
                self.assertNotIn("PRIVATE", "\n".join(lines))
                for process_group in verifier.stopped_service_process_groups():
                    self.assertFalse(verifier._group_exists(process_group))

    @acceptance
    def test_real_disposable_coordinator_recovers_and_continues(self):
        """The acceptance uses the production updater/checker, not marker doubles."""
        import scripts.verify_fleet_update_workflow as verifier

        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertNotIn("class _FixtureUpdater", source)
        self.assertNotIn("class _FixtureChecker", source)

        with tempfile.TemporaryDirectory() as temporary:
            fleet = verifier._DisposableFleet(Path(temporary))
            try:
                fleet.create()
                paths = HostProfilePaths.for_repository(fleet.platform)
                profile = HostProfileStore(paths)
                self.assertEqual(fleet.registry, paths.profile)
                self.assertEqual(len(profile.revisions()), 1)
                self.assertEqual(profile.read_current(), paths.profile.read_bytes())
                self.assertEqual(stat.S_IMODE(paths.local.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.history.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.backups.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.profile.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(next(paths.history.iterdir()).stat().st_mode),
                    0o600,
                )
                fleet.preview()
                self.assertEqual(
                    fleet.cli_preview_statuses(),
                    (
                        ("current-static", "CURRENT"),
                        ("ready-static", "READY"),
                        ("legacy", "SKIPPED"),
                    ),
                )
                self.assertEqual(profile.read_current(), fleet.registry.read_bytes())
                self.assertEqual(len(profile.revisions()), 3)
                result = fleet.apply()

                self.assertEqual(
                    [(item.app_id, item.status) for item in result.apps],
                    [
                        ("current-static", "CURRENT"),
                        ("ready-static", "UPDATED"),
                        ("ready-service", "UPDATED"),
                        ("failing-static", "FAILED_RECOVERED"),
                        ("later-static", "UPDATED"),
                        ("legacy", "SKIPPED"),
                    ],
                )
                self.assertTrue(fleet.checks_ran)
                self.assertTrue(fleet.releases_match_commits())
                self.assertTrue(fleet.legacy_is_untouched())
                self.assertTrue(fleet.retry_is_noop())
                process_group = fleet.service.process_group
            finally:
                fleet.close()
            self.assertIsNotNone(process_group)
            self.assertFalse(verifier._group_exists(process_group))

    def test_public_output_never_exposes_private_fixture_details(self):
        import scripts.verify_fleet_update_workflow as verifier

        output = io.StringIO()
        with (
            patch.object(verifier, "verify", return_value=verifier.EXPECTED_PHASES),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(verifier.main(), 0)
        rendered = output.getvalue()
        self.assertEqual(
            rendered.splitlines(), list(verifier.EXPECTED_PHASES)
        )
        for forbidden in ("PRIVATE", "fixture-command", "ENV_MARKER", tempfile.gettempdir()):
            self.assertNotIn(forbidden, rendered)

    @acceptance
    def test_real_late_cleanup_failure_removes_owned_resources(self):
        import scripts.verify_fleet_update_workflow as verifier

        roots: list[Path] = []
        lines: list[str] = []
        verifier_instance = verifier.FleetUpdateWorkflowVerifier(
            temporary_directory_factory=verifier.RecordingTemporaryDirectory(roots),
            fail_after=verifier.EXPECTED_PHASES[-1],
            emit=lines.append,
        )

        self.assertEqual(verifier_instance.run(), 1)
        self.assertEqual(
            lines,
            [
                *verifier.EXPECTED_PHASES[:-1],
                verifier.EXPECTED_PHASES[-1].replace(" PASS", " FAIL"),
            ],
        )
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        for process_group in verifier.stopped_service_process_groups():
            self.assertFalse(verifier._group_exists(process_group))

    @acceptance
    def test_bounded_runner_reaps_descendants_after_every_outcome(self):
        """A dead leader must not allow an inherited-pipe descendant to survive."""
        import scripts.verify_fleet_update_workflow as verifier

        for outcome in ("success", "nonzero", "timeout", "overflow", "failure"):
            with self.subTest(outcome=outcome):
                result = verifier.run_bounded_probe(outcome)
                self.assertEqual(result, outcome)
                self.assertTrue(verifier.probe_process_group_reaped())
                self.assertTrue(verifier.probe_descendant_reaped())

    @acceptance
    def test_recovery_requires_the_exact_original_tree_and_platform_bytes(self):
        import scripts.verify_fleet_update_workflow as verifier

        with tempfile.TemporaryDirectory() as temporary:
            fleet = verifier._DisposableFleet(
                Path(temporary), require_cli_preview=False
            )
            try:
                fleet.create()
                original = fleet.recovery_original()
                fleet.apply()
                self.assertTrue(fleet.recovery_matches_original())
                recovered = fleet.apps / "failing-static"
                release_key = recovered.resolve()
                original_release = fleet.releases[release_key]
                fleet.releases[release_key] = "wrong-live-release"
                self.assertFalse(fleet.recovery_matches(original))
                fleet.releases[release_key] = original_release
                self.assertTrue(fleet.recovery_matches(original))
                (recovered / "domain-note.txt").write_text("different but valid\n")
                verifier._git(recovered, "add", "domain-note.txt")
                verifier._git(
                    recovered,
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "commit",
                    "-m",
                    "different clean state",
                )
                verifier._RecordingChecker(fleet.checked).check(recovered)
                self.assertFalse(
                    fleet.recovery_matches(original)
                )
            finally:
                fleet.close()

    @acceptance
    def test_service_activation_requires_exact_release_and_declared_command(self):
        import scripts.verify_fleet_update_workflow as verifier

        with tempfile.TemporaryDirectory() as temporary:
            fleet = verifier._DisposableFleet(
                Path(temporary), require_cli_preview=False
            )
            try:
                fleet.create()
                service = fleet.apps / "ready-service"
                verifier._RecordingChecker(fleet.checked).check(service)
                self.assertIsNotNone(fleet.activator)
                fleet.activator.activate(service)  # type: ignore[union-attr]
                process_group = fleet.service.process_group
                self.assertIsNotNone(process_group)
                fleet.activator.activate(service)  # type: ignore[union-attr]
                self.assertEqual(fleet.service.process_group, process_group)
                release_source = service / "release/server/service.py"
                original = release_source.read_bytes()
                release_source.write_bytes(b"# mismatched release\n")
                with self.assertRaises(RuntimeError):
                    fleet.activator.activate(service)  # type: ignore[union-attr]
                release_source.write_bytes(original)

                manifest_path = service / "local-web.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["service"]["startCommand"].insert(2, "-u")
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    fleet.activator.activate(service)  # type: ignore[union-attr]
            finally:
                fleet.close()
