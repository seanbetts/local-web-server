import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore

import scripts.verify_foundation_adoption_workflow as foundation_verifier


EXPECTED_LABELS = (
    "create generic legacy service fixture",
    "preview React foundation without writes",
    "apply exact platform-owned foundation files",
    "preserve Python service legacy frontend and repository data",
    "commit adopted foundation and pass Doctor",
    "run foundation-local check and browser suite",
    "reject legacy release outcome",
    "integrate app-owned React release",
    "pass full app check and activation eligibility",
    "cleanup",
)

INJECTED_FAILURE_POINTS = (
    "preview React foundation without writes",
    "apply exact platform-owned foundation files",
    "reject legacy release outcome",
    "pass full app check and activation eligibility",
)

REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory
REAL_POPEN = subprocess.Popen


class RecordingTemporaryDirectory:
    created: list[Path] = []
    process_ledgers: list[dict[str, object]] = []

    def __init__(self, *args, **kwargs):
        self._temporary = REAL_TEMPORARY_DIRECTORY(*args, **kwargs)
        self.name = self._temporary.name
        self.created.append(Path(self.name))

    def __enter__(self):
        return self.name

    def __exit__(self, *error):
        return self._temporary.__exit__(*error)

    def cleanup(self):
        root = Path(self.name)
        if root.exists():
            for ledger in sorted(root.glob(".direct-seam-ledger-*.json")):
                content = ledger.read_bytes()
                if len(content) <= 16 * 1024:
                    self.process_ledgers.append(json.loads(content))
        return self._temporary.cleanup()


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False


def _assert_nested_process_ledgers(
    test: unittest.TestCase, ledgers: list[dict[str, object]]
) -> None:
    test.assertTrue(ledgers)
    entries: list[tuple[int, int, bool, int]] = []
    for ledger in ledgers:
        test.assertEqual(
            set(ledger), {"schemaVersion", "workerPgid", "processes"}
        )
        test.assertEqual(ledger["schemaVersion"], 1)
        worker_group = ledger["workerPgid"]
        test.assertIs(type(worker_group), int)
        processes = ledger["processes"]
        test.assertIsInstance(processes, list)
        for process in processes:
            test.assertEqual(
                set(process), {"pid", "pgid", "startNewSession"}
            )
            entries.append(
                (
                    process["pid"],
                    process["pgid"],
                    process["startNewSession"],
                    worker_group,
                )
            )
    test.assertTrue(entries)
    test.assertTrue(
        any(
            started_session
            for _pid, _pgid, started_session, _worker in entries
        )
    )
    test.assertTrue(
        any(
            not started_session
            for _pid, _pgid, started_session, _worker in entries
        )
    )
    for pid, process_group, started_session, worker_group in entries:
        test.assertIs(type(pid), int)
        test.assertIs(type(process_group), int)
        test.assertIs(type(started_session), bool)
        test.assertEqual(process_group, pid if started_session else worker_group)
        test.assertFalse(_process_group_exists(process_group))
    for ledger in ledgers:
        test.assertFalse(_process_group_exists(ledger["workerPgid"]))


class FoundationAdoptionWorkflowTests(unittest.TestCase):
    def test_package_exposes_the_real_foundation_adoption_acceptance(self):
        package = json.loads(
            (Path(__file__).parents[1] / "package.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            package["scripts"]["verify:foundation-adoption"],
            "python3 scripts/verify_foundation_adoption_workflow.py",
        )

    def test_disposable_registry_helper_initialises_a_private_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            platform = root / "platform"
            platform.mkdir()
            (platform / "config").mkdir()
            (platform / "local_web_server").mkdir()
            (platform / "local_web_server/source.py").write_text(
                "# disposable\n", encoding="utf-8"
            )
            (platform / ".gitignore").write_text(
                "config/local/\n", encoding="utf-8"
            )
            subprocess.run(
                ("/usr/bin/git", "init", "-q", "-b", "main"),
                cwd=platform,
                check=True,
            )
            subprocess.run(
                ("/usr/bin/git", "add", "."), cwd=platform, check=True
            )
            subprocess.run(
                (
                    "/usr/bin/git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ),
                cwd=platform,
                check=True,
            )

            registry, expected = foundation_verifier._write_disposable_registry(
                platform, root / "runtime"
            )
            paths = HostProfilePaths.for_repository(platform)
            store = HostProfileStore(paths)

            self.assertEqual(registry, paths.profile)
            self.assertEqual(store.read_current(), expected)
            self.assertEqual(len(store.revisions()), 1)
            self.assertEqual(stat.S_IMODE(paths.local.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(paths.profile.stat().st_mode), 0o600)

    def test_verify_returns_the_exact_ordered_bounded_evidence(self):
        """Dropping, renaming, or reordering a real acceptance phase must fail."""

        RecordingTemporaryDirectory.created = []
        RecordingTemporaryDirectory.process_ledgers = []
        with patch.object(
            foundation_verifier,
            "TemporaryDirectory",
            RecordingTemporaryDirectory,
        ):
            labels = foundation_verifier.verify()

        self.assertEqual(labels, EXPECTED_LABELS)
        _assert_nested_process_ledgers(
            self, RecordingTemporaryDirectory.process_ledgers
        )

    def test_preview_phase_accepts_the_generated_context_adapter_paths(self):
        """A current foundation preview includes its managed context adapter files."""

        with tempfile.TemporaryDirectory(
            prefix="local-web-foundation-preview-contract-"
        ) as temporary_root:
            platform, repository, _preserved = (
                foundation_verifier._create_fixture_phase(Path(temporary_root))
            )

            foundation_verifier._preview_phase(platform, repository)

    def test_public_phase_stays_exact_while_private_check_diagnostic_is_bounded(
        self,
    ):
        output = io.StringIO()
        with (
            patch.object(foundation_verifier, "verify", return_value=EXPECTED_LABELS),
            contextlib.redirect_stdout(output),
        ):
            result = foundation_verifier.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [f"{label} PASS" for label in EXPECTED_LABELS],
        )

        private_marker = "private-check-output-and-application-data"
        private_path = "/private/foundation-check-fixture"
        command_indexes = {
            "npm-ci": 0,
            "npm-check": 1,
            "npm-e2e": 2,
        }

        def command_failure(outcome: str) -> RuntimeError:
            if outcome == "timeout":
                error = RuntimeError("disposable child command failed")
                error.__cause__ = subprocess.TimeoutExpired(
                    (private_path, private_marker),
                    300,
                    output=private_marker.encode(),
                    stderr=private_marker.encode(),
                )
                return error
            if outcome == "output-bound":
                error = RuntimeError(
                    "disposable child output exceeded its bound"
                )
                error.__cause__ = RuntimeError(private_marker)
                return error
            if outcome == "cleanup":
                cleanup_error = RuntimeError(
                    "disposable process cleanup failed"
                )
                cleanup_error.__cause__ = RuntimeError(
                    f"{private_marker} {private_path}"
                )
                error = RuntimeError("disposable child command failed")
                error.__cause__ = cleanup_error
                return error
            return RuntimeError(
                f"disposable child command failed: {private_marker} {private_path}"
            )

        for command, command_index in command_indexes.items():
            for outcome in ("nonzero", "timeout", "output-bound", "cleanup"):
                with self.subTest(command=command, outcome=outcome):
                    labels: list[str] = []
                    side_effects: list[object] = [
                        ("", "") for _index in range(command_index)
                    ]
                    side_effects.append(command_failure(outcome))
                    with (
                        patch.object(
                            foundation_verifier,
                            "_run",
                            side_effect=side_effects,
                        ),
                        self.assertRaises(
                            foundation_verifier.FoundationAdoptionError
                        ) as caught,
                    ):
                        foundation_verifier._phase(
                            EXPECTED_LABELS[5],
                            lambda: foundation_verifier._foundation_checks_phase(
                                Path(private_path)
                            ),
                            labels,
                            None,
                        )

                    self.assertEqual(str(caught.exception), EXPECTED_LABELS[5])
                    self.assertNotIn(private_marker, str(caught.exception))
                    self.assertNotIn(private_path, str(caught.exception))
                    diagnostic = caught.exception.__cause__
                    self.assertEqual(
                        type(diagnostic).__name__, "_FoundationCheckDiagnostic"
                    )
                    self.assertEqual(getattr(diagnostic, "command", None), command)
                    self.assertEqual(getattr(diagnostic, "outcome", None), outcome)
                    self.assertEqual(str(diagnostic), f"{command}:{outcome}")
                    self.assertNotIn(private_marker, str(diagnostic))
                    self.assertNotIn(private_path, str(diagnostic))
                    self.assertIsNone(diagnostic.__cause__)
                    self.assertIsNone(diagnostic.__context__)
                    self.assertEqual(
                        set(diagnostic.__dict__), {"command", "outcome"}
                    )
                    self.assertEqual(labels, [])

    def test_run_uses_a_disposable_private_state_boundary_in_every_outcome(self):
        """The command boundary must isolate host state and remove owned state."""

        private_environment_marker = "private-host-environment-and-npm-config"
        private_output_marker = "private-owned-child-output"
        process_groups: list[int] = []
        real_stop_process_group = foundation_verifier._stop_process_group
        expected_keys = {
            "PATH",
            "LANG",
            "LC_ALL",
            "CI",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_OPTIONAL_LOCKS",
            "GIT_TERMINAL_PROMPT",
            "PYTHONDONTWRITEBYTECODE",
            "HOME",
            "TMPDIR",
            "NPM_CONFIG_USERCONFIG",
            "NPM_CONFIG_GLOBALCONFIG",
            "NPM_CONFIG_CACHE",
            "PLAYWRIGHT_BROWSERS_PATH",
            "__CF_USER_TEXT_ENCODING",
        }
        nested_expected_keys = {
            "PATH",
            "LANG",
            "LC_ALL",
            "CI",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_TERMINAL_PROMPT",
            "HOME",
            "TMPDIR",
            "NPM_CONFIG_USERCONFIG",
            "NPM_CONFIG_GLOBALCONFIG",
            "NPM_CONFIG_CACHE",
            "PLAYWRIGHT_BROWSERS_PATH",
            "__CF_USER_TEXT_ENCODING",
        }
        inspect_environment = (
            "import json,os,stat,sys,time\n"
            "from pathlib import Path\n"
            "home=Path(os.environ['HOME'])\n"
            "configs=(home/'.npmrc',Path(os.environ['NPM_CONFIG_USERCONFIG']),"
            "Path(os.environ['NPM_CONFIG_GLOBALCONFIG']))\n"
            "mounts=(home/'.npm',home/'Library'/'Caches'/'ms-playwright')\n"
            "payload={'environment':dict(os.environ),"
            "'homeMode':stat.S_IMODE(home.stat().st_mode),"
            "'configuredFilesReadable':[p.is_file() and os.access(p,os.R_OK) "
            "for p in configs],"
            "'mountsAreSymlinks':[p.is_symlink() for p in mounts],"
            "'mountTargets':[str(p.resolve()) if p.exists() else None "
            "for p in mounts],"
            "'mountParentModes':[stat.S_IMODE(p.stat().st_mode) if p.exists() "
            "else None for p in (home,home/'Library',home/'Library'/'Caches')]}\n"
            "Path(sys.argv[1]).write_text(json.dumps(payload))\n"
            "outcome=sys.argv[2]\n"
            "if outcome=='nonzero': raise SystemExit(7)\n"
            "if outcome=='timeout': time.sleep(4)\n"
            "if outcome=='output-bound':\n"
            f" os.write(1,{private_output_marker!r}.encode()*256)\n"
            " time.sleep(4)\n"
        )
        inspect_nested_environment = (
            "import json,os,stat,sys\n"
            "from pathlib import Path\n"
            "home=Path(os.environ['HOME'])\n"
            "configs=(home/'.npmrc',Path(os.environ['NPM_CONFIG_USERCONFIG']),"
            "Path(os.environ['NPM_CONFIG_GLOBALCONFIG']))\n"
            "caches=(Path(os.environ['NPM_CONFIG_CACHE']),"
            "Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']))\n"
            "cache_root=caches[0].parent\n"
            "payload={'environment':dict(os.environ),"
            "'homeMode':stat.S_IMODE(home.stat().st_mode),"
            "'configuredFilesReadable':[p.is_file() and os.access(p,os.R_OK) "
            "for p in configs],"
            "'cacheMountsAreSymlinks':[p.is_symlink() for p in caches],"
            "'cacheTargets':[str(p.resolve()) if p.exists() else None "
            "for p in caches],"
            "'cacheParentModes':[stat.S_IMODE(p.stat().st_mode) if p.exists() "
            "else None for p in (cache_root,cache_root/'Library',"
            "cache_root/'Library'/'Caches')]}\n"
            "Path(sys.argv[1]).write_text(json.dumps(payload))\n"
        )
        run_nested_process_runner = (
            "import sys\n"
            "from pathlib import Path\n"
            "import scripts.verify_foundation_adoption_workflow as verifier\n"
            "from local_web_server.process_runner import "
            "ProcessCommand,ProcessRunner\n"
            "ProcessRunner().run(Path(sys.argv[1]).parent,(ProcessCommand("
            "'nested-probe',(sys.executable,'-c',sys.argv[2],sys.argv[1])),))\n"
        )

        def recording_popen(*args, **kwargs):
            process = REAL_POPEN(*args, **kwargs)
            process_groups.append(process.pid)
            return process

        def stop_then_fail(process):
            real_stop_process_group(process)
            raise RuntimeError("disposable process cleanup failed")

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            repository = root / "repository"
            repository.mkdir()
            trusted_npm_cache = root / "trusted-npm-cache"
            trusted_playwright_cache = root / "trusted-playwright-cache"
            trusted_npm_cache.mkdir()
            trusted_playwright_cache.mkdir()
            trusted_npm_target = trusted_npm_cache.resolve(strict=True)
            trusted_playwright_target = trusted_playwright_cache.resolve(
                strict=True
            )
            npm_sentinel = trusted_npm_cache / "sentinel.txt"
            playwright_sentinel = trusted_playwright_cache / "sentinel.txt"
            npm_sentinel.write_text("trusted npm cache sentinel")
            playwright_sentinel.write_text("trusted browser cache sentinel")
            host_home = root / "seeded-host-home"
            host_home.mkdir()
            host_home.chmod(0o755)
            host_user_config = host_home / "private-user.npmrc"
            host_global_config = host_home / "private-global.npmrc"
            host_npmrc = host_home / ".npmrc"
            for private_config in (
                host_user_config,
                host_global_config,
                host_npmrc,
            ):
                private_config.write_text(private_environment_marker)
            seeded_environment = {
                "HOME": str(host_home),
                "TMPDIR": str(root / "private-host-tmp"),
                "PATH": str(root / "private-host-bin"),
                "PRIVATE_PROVIDER_TOKEN": private_environment_marker,
                "NPM_CONFIG_USERCONFIG": str(host_user_config),
                "npm_config_userconfig": str(host_user_config),
                "NPM_CONFIG_GLOBALCONFIG": str(host_global_config),
                "npm_config_globalconfig": str(host_global_config),
                "NPM_CONFIG_CACHE": str(root / "private-host-npm-cache"),
                "npm_config_cache": str(root / "private-host-npm-cache"),
                "PLAYWRIGHT_BROWSERS_PATH": str(
                    root / "private-host-playwright-cache"
                ),
                "GIT_CONFIG_GLOBAL": str(root / "private-host-gitconfig"),
                "__CF_USER_TEXT_ENCODING": private_environment_marker,
            }
            payload_names: set[str] = set()

            for outcome in (
                "success",
                "nonzero",
                "timeout",
                "output-bound",
                "cleanup",
            ):
                with self.subTest(outcome=outcome):
                    payload_path = repository / f"{outcome}.json"
                    payload_names.add(payload_path.name)
                    cleanup_context = (
                        patch.object(
                            foundation_verifier,
                            "_stop_process_group",
                            side_effect=stop_then_fail,
                        )
                        if outcome == "cleanup"
                        else contextlib.nullcontext()
                    )
                    started = time.monotonic()
                    caught: RuntimeError | None = None
                    with (
                        patch.dict(os.environ, seeded_environment),
                        patch.object(
                            foundation_verifier,
                            "_MAX_CAPTURE_BYTES",
                            1024,
                        ),
                        patch.object(
                            foundation_verifier,
                            "_NPM_CACHE",
                            trusted_npm_cache,
                        ),
                        patch.object(
                            foundation_verifier,
                            "_PLAYWRIGHT_CACHE",
                            trusted_playwright_cache,
                        ),
                        patch.object(
                            foundation_verifier.subprocess,
                            "Popen",
                            side_effect=recording_popen,
                        ),
                        cleanup_context,
                    ):
                        try:
                            foundation_verifier._run(
                                (
                                    sys.executable,
                                    "-c",
                                    inspect_environment,
                                    str(payload_path),
                                    outcome,
                                ),
                                cwd=repository,
                                timeout=(
                                    0.1 if outcome == "timeout" else 8
                                ),
                                capture=outcome == "output-bound",
                            )
                        except RuntimeError as error:
                            caught = error
                    elapsed = time.monotonic() - started

                    if outcome == "success":
                        self.assertIsNone(caught)
                    else:
                        self.assertIsNotNone(caught)
                        expected_error = (
                            "disposable child output exceeded its bound"
                            if outcome == "output-bound"
                            else "disposable child command failed"
                        )
                        self.assertEqual(str(caught), expected_error)
                        self.assertNotIn(
                            private_environment_marker, str(caught)
                        )
                        self.assertNotIn(private_output_marker, str(caught))
                        self.assertNotIn(str(root), str(caught))
                    if outcome == "output-bound":
                        self.assertLess(elapsed, 2)

                    payload = json.loads(payload_path.read_text())
                    environment = payload["environment"]
                    self.assertEqual(set(environment), expected_keys)
                    self.assertEqual(
                        environment["PATH"],
                        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    )
                    self.assertEqual(environment["LANG"], "C")
                    self.assertEqual(environment["LC_ALL"], "C")
                    self.assertEqual(environment["CI"], "1")
                    self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
                    self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
                    self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
                    self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
                    self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
                    self.assertEqual(
                        environment["PYTHONDONTWRITEBYTECODE"], "1"
                    )
                    command_home = Path(environment["HOME"])
                    self.assertEqual(environment["TMPDIR"], str(command_home))
                    self.assertEqual(payload["homeMode"], 0o700)
                    self.assertEqual(
                        environment["NPM_CONFIG_USERCONFIG"],
                        str(command_home / ".npmrc-user-disabled"),
                    )
                    self.assertEqual(
                        environment["NPM_CONFIG_GLOBALCONFIG"],
                        str(command_home / ".npmrc-global-disabled"),
                    )
                    self.assertEqual(
                        environment["NPM_CONFIG_CACHE"],
                        str(trusted_npm_cache),
                    )
                    self.assertEqual(
                        environment["PLAYWRIGHT_BROWSERS_PATH"],
                        str(trusted_playwright_cache),
                    )
                    self.assertNotEqual(
                        environment["__CF_USER_TEXT_ENCODING"],
                        private_environment_marker,
                    )
                    self.assertEqual(
                        payload["configuredFilesReadable"],
                        [False, False, False],
                    )
                    self.assertEqual(
                        payload["mountsAreSymlinks"], [True, True]
                    )
                    self.assertEqual(
                        payload["mountTargets"],
                        [
                            str(trusted_npm_target),
                            str(trusted_playwright_target),
                        ],
                    )
                    self.assertEqual(
                        payload["mountParentModes"],
                        [0o700, 0o700, 0o700],
                    )
                    serialized_payload = json.dumps(payload)
                    self.assertNotIn(
                        private_environment_marker, serialized_payload
                    )
                    self.assertNotIn(str(host_home), serialized_payload)
                    self.assertFalse(command_home.exists())
                    self.assertEqual(
                        npm_sentinel.read_text(), "trusted npm cache sentinel"
                    )
                    self.assertEqual(
                        playwright_sentinel.read_text(),
                        "trusted browser cache sentinel",
                    )

            nested_payload_path = repository / "nested.json"
            payload_names.add(nested_payload_path.name)
            nested_error: RuntimeError | None = None
            with (
                patch.dict(os.environ, seeded_environment),
                patch.object(
                    foundation_verifier,
                    "_NPM_CACHE",
                    trusted_npm_cache,
                ),
                patch.object(
                    foundation_verifier,
                    "_PLAYWRIGHT_CACHE",
                    trusted_playwright_cache,
                ),
                patch.object(
                    foundation_verifier.subprocess,
                    "Popen",
                    side_effect=recording_popen,
                ),
            ):
                try:
                    foundation_verifier._run(
                        (
                            sys.executable,
                            "-c",
                            run_nested_process_runner,
                            str(nested_payload_path),
                            inspect_nested_environment,
                        ),
                        cwd=foundation_verifier.PLATFORM_REPOSITORY,
                        timeout=30,
                    )
                except RuntimeError as error:
                    nested_error = error
            self.assertIsNone(nested_error)

            nested_payload = json.loads(nested_payload_path.read_text())
            nested_environment = nested_payload["environment"]
            self.assertEqual(set(nested_environment), nested_expected_keys)
            nested_npm_cache = Path(nested_environment["NPM_CONFIG_CACHE"])
            nested_playwright_cache = Path(
                nested_environment["PLAYWRIGHT_BROWSERS_PATH"]
            )
            self.assertNotEqual(nested_npm_cache, trusted_npm_cache)
            self.assertNotEqual(
                nested_playwright_cache, trusted_playwright_cache
            )
            self.assertEqual(nested_npm_cache.name, ".npm")
            self.assertEqual(
                nested_playwright_cache.parts[-3:],
                ("Library", "Caches", "ms-playwright"),
            )
            self.assertEqual(
                nested_payload["cacheMountsAreSymlinks"], [True, True]
            )
            self.assertEqual(
                nested_payload["cacheTargets"],
                [str(trusted_npm_target), str(trusted_playwright_target)],
            )
            self.assertEqual(
                nested_payload["cacheParentModes"], [0o700, 0o700, 0o700]
            )
            nested_home = Path(nested_environment["HOME"])
            self.assertEqual(nested_environment["TMPDIR"], str(nested_home))
            self.assertEqual(nested_payload["homeMode"], 0o700)
            self.assertEqual(
                nested_payload["configuredFilesReadable"],
                [False, False, False],
            )
            nested_serialized = json.dumps(nested_payload)
            self.assertNotIn(private_environment_marker, nested_serialized)
            self.assertNotIn(str(host_home), nested_serialized)
            self.assertFalse(nested_home.exists())
            self.assertFalse(nested_npm_cache.parent.exists())
            self.assertEqual(
                npm_sentinel.read_text(), "trusted npm cache sentinel"
            )
            self.assertEqual(
                playwright_sentinel.read_text(),
                "trusted browser cache sentinel",
            )

            unsafe_cache_target = root / "unsafe-cache-symlink"
            unsafe_cache_target.symlink_to(
                trusted_npm_cache, target_is_directory=True
            )
            invalid_cache_targets = {
                "missing": root / "missing-cache-target",
                "non-absolute": Path("relative-cache-target"),
                "unsafe": unsafe_cache_target,
            }
            for invalid_name, invalid_target in invalid_cache_targets.items():
                with self.subTest(invalid_cache_target=invalid_name):
                    RecordingTemporaryDirectory.created = []
                    process_count = len(process_groups)
                    with (
                        patch.object(
                            foundation_verifier,
                            "_NPM_CACHE",
                            invalid_target,
                        ),
                        patch.object(
                            foundation_verifier,
                            "_PLAYWRIGHT_CACHE",
                            trusted_playwright_cache,
                        ),
                        patch.object(
                            foundation_verifier.tempfile,
                            "TemporaryDirectory",
                            RecordingTemporaryDirectory,
                        ),
                        patch.object(
                            foundation_verifier.subprocess,
                            "Popen",
                            side_effect=recording_popen,
                        ),
                        self.assertRaises(RuntimeError) as invalid_caught,
                    ):
                        foundation_verifier._run(
                            (sys.executable, "-c", "pass"),
                            cwd=repository,
                            timeout=30,
                        )
                    self.assertEqual(
                        str(invalid_caught.exception),
                        "disposable cache mounts were invalid",
                    )
                    self.assertNotIn(
                        private_environment_marker,
                        str(invalid_caught.exception),
                    )
                    self.assertNotIn(
                        str(root), str(invalid_caught.exception)
                    )
                    self.assertEqual(len(process_groups), process_count)
                    self.assertTrue(RecordingTemporaryDirectory.created)
                    for command_home in RecordingTemporaryDirectory.created:
                        self.assertFalse(command_home.exists())
                    self.assertEqual(
                        npm_sentinel.read_text(), "trusted npm cache sentinel"
                    )
                    self.assertEqual(
                        playwright_sentinel.read_text(),
                        "trusted browser cache sentinel",
                    )

            self.assertEqual(
                {path.name for path in repository.iterdir()}, payload_names
            )
        self.assertTrue(process_groups)
        for process_group in process_groups:
            self.assertFalse(_process_group_exists(process_group))

    def test_tree_snapshot_detects_non_file_and_metadata_mutations(self):
        """Dry-run and publication comparisons must observe the full tree."""

        def changed_paths_after(mutation):
            with tempfile.TemporaryDirectory() as temporary_root:
                root = Path(temporary_root)
                target_one = root / "target-one"
                target_two = root / "target-two"
                target_one.write_bytes(b"same bytes\n")
                target_two.write_bytes(b"same bytes\n")
                changing = root / "changing"
                mutation("prepare", changing, target_one, target_two)
                before = foundation_verifier._snapshot_tree(root)
                mutation("change", changing, target_one, target_two)
                after = foundation_verifier._snapshot_tree(root)
                return foundation_verifier._changed_paths(before, after)

        mutations = {
            "empty directory creation": (
                lambda stage, changing, _one, _two: (
                    changing.mkdir() if stage == "change" else None
                )
            ),
            "entry type change": (
                lambda stage, changing, one, _two: (
                    changing.write_bytes(b"same bytes\n")
                    if stage == "prepare"
                    else (changing.unlink(), changing.symlink_to(one))
                )
            ),
            "symlink target change": (
                lambda stage, changing, one, two: (
                    changing.symlink_to(one)
                    if stage == "prepare"
                    else (changing.unlink(), changing.symlink_to(two))
                )
            ),
            "mode-only mutation": (
                lambda stage, changing, _one, _two: (
                    (changing.write_bytes(b"mode bytes\n"), changing.chmod(0o644))
                    if stage == "prepare"
                    else changing.chmod(0o600)
                )
            ),
        }

        for mutation_name, mutation in mutations.items():
            with self.subTest(mutation=mutation_name):
                self.assertEqual(
                    changed_paths_after(mutation), frozenset({Path("changing")})
                )

    def test_every_popen_is_grouped_and_injected_failures_clean_owned_state(self):
        """Every verifier child is grouped and gone after each injected failure."""

        for fail_after in INJECTED_FAILURE_POINTS:
            with self.subTest(fail_after=fail_after):
                RecordingTemporaryDirectory.created = []
                RecordingTemporaryDirectory.process_ledgers = []
                processes: list[tuple[int, bool, tuple[str, ...]]] = []

                def recording_popen(*args, **kwargs):
                    process = REAL_POPEN(*args, **kwargs)
                    command = args[0] if args else kwargs["args"]
                    processes.append(
                        (
                            process.pid,
                            kwargs.get("start_new_session") is True,
                            tuple(str(part) for part in command),
                        )
                    )
                    return process

                with (
                    patch.object(
                        foundation_verifier,
                        "TemporaryDirectory",
                        RecordingTemporaryDirectory,
                    ),
                    patch.object(
                        foundation_verifier.subprocess,
                        "Popen",
                        side_effect=recording_popen,
                    ),
                    self.assertRaises(
                        foundation_verifier.FoundationAdoptionError
                    ) as caught,
                ):
                    foundation_verifier.verify(fail_after=fail_after)

                diagnostic = caught.exception.__cause__
                safe_diagnostic = (
                    str(diagnostic)
                    if isinstance(
                        diagnostic,
                        foundation_verifier._FoundationCheckDiagnostic,
                    )
                    else None
                )
                self.assertEqual(
                    str(caught.exception), fail_after, safe_diagnostic
                )
                self.assertTrue(RecordingTemporaryDirectory.created)
                self.assertTrue(processes)

                def is_disposable_profile_git_probe(
                    command: tuple[str, ...]
                ) -> bool:
                    if (
                        len(command) < 5
                        or command[0] not in {"git", "/usr/bin/git"}
                        or command[1] != "-C"
                    ):
                        return False
                    repository = Path(command[2])
                    if not any(
                        repository.is_relative_to(root.resolve())
                        for root in RecordingTemporaryDirectory.created
                    ):
                        return False
                    arguments = command[3:]
                    return arguments in {
                        ("rev-parse", "--show-toplevel"),
                        ("symbolic-ref", "--quiet", "--short", "HEAD"),
                        ("rev-parse", "--verify", "HEAD"),
                    } or arguments[:4] == (
                        "--literal-pathspecs",
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    )

                self.assertTrue(
                    all(
                        grouped or is_disposable_profile_git_probe(command)
                        for _process, grouped, command in processes
                    ),
                    "every long-lived verifier Popen must own its process group",
                )
                for temporary_root in RecordingTemporaryDirectory.created:
                    self.assertFalse(temporary_root.exists())
                    self.assertNotIn(str(temporary_root), str(caught.exception))
                for process_group, _grouped, _command in processes:
                    self.assertFalse(_process_group_exists(process_group))
                if fail_after in {
                    "reject legacy release outcome",
                    "pass full app check and activation eligibility",
                }:
                    _assert_nested_process_ledgers(
                        self, RecordingTemporaryDirectory.process_ledgers
                    )
                else:
                    self.assertEqual(
                        RecordingTemporaryDirectory.process_ledgers, []
                    )


if __name__ == "__main__":
    unittest.main()
