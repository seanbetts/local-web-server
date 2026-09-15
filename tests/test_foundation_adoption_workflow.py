import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.suites import acceptance
import scripts.verify_foundation_adoption_workflow as foundation_verifier


class FoundationAdoptionWorkflowTests(unittest.TestCase):
    @acceptance
    def test_real_foundation_adoption_preserves_domain_data_and_cleans_up(self):
        # The verifier checks preview purity, preserved domain files, the generated
        # frontend, release eligibility, and process cleanup through real tools.
        roots = []
        real_temporary = tempfile.TemporaryDirectory

        def temporary(*args, **kwargs):
            result = real_temporary(*args, **kwargs)
            roots.append(Path(result.name))
            return result

        with patch.object(foundation_verifier, "TemporaryDirectory", side_effect=temporary):
            foundation_verifier.verify()
        self.assertTrue(roots)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_check_failure_diagnostics_exclude_private_child_details(self):
        private = "PRIVATE child output /private/application"
        # Phase/error text is incidental; neither the public error nor its
        # bounded diagnostic may retain the original exception's private data.
        timeout = RuntimeError("disposable child command failed")
        timeout.__cause__ = subprocess.TimeoutExpired((private,), 1, output=private.encode())
        for error in (RuntimeError(private), timeout):
            with self.subTest(error=type(error).__name__):
                with patch.object(foundation_verifier, "_run", side_effect=error):
                    with self.assertRaises(foundation_verifier.FoundationAdoptionError) as caught:
                        foundation_verifier._phase(
                            foundation_verifier.PASS_LABELS[5],
                            lambda: foundation_verifier._foundation_checks_phase(Path("/private/application")),
                            [], None,
                        )
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertNotIn("/private/application", str(caught.exception))
                diagnostic = caught.exception.__cause__
                self.assertIsNotNone(diagnostic)
                self.assertNotIn("PRIVATE", str(diagnostic))
                self.assertNotIn("/private/application", str(diagnostic))
                self.assertIsNone(diagnostic.__cause__)
                self.assertIsNone(diagnostic.__context__)

    @acceptance
    def test_children_and_nested_runner_use_private_homes_and_trusted_caches(self):
        probe = """
import json, os, stat, sys
from pathlib import Path
home = Path(os.environ['HOME'])
configs = (home/'.npmrc', Path(os.environ['NPM_CONFIG_USERCONFIG']), Path(os.environ['NPM_CONFIG_GLOBALCONFIG']))
caches = (Path(os.environ['NPM_CONFIG_CACHE']), Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']))
Path(sys.argv[1]).write_text(json.dumps({
    'environment': dict(os.environ), 'homeMode': stat.S_IMODE(home.stat().st_mode),
    'configsReadable': [p.is_file() and os.access(p, os.R_OK) for p in configs],
    'cacheTargets': [str(p.resolve(strict=True)) for p in caches],
}))
"""
        nested = """
import sys
from pathlib import Path
import scripts.verify_foundation_adoption_workflow
from local_web_server.process_runner import ProcessCommand, ProcessRunner
ProcessRunner().run(Path(sys.argv[1]).parent, (ProcessCommand('nested-probe', (sys.executable, '-c', sys.argv[2], sys.argv[1])),))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            host = root / "host"
            host.mkdir()
            (host / ".npmrc").write_text("PRIVATE_TOKEN")
            caches = [root / "npm", root / "browsers"]
            for cache in caches:
                cache.mkdir()
                (cache / "sentinel").write_text("keep")
            environment = {
                "HOME": str(host), "PRIVATE_PROVIDER_TOKEN": "PRIVATE_TOKEN",
                "NPM_CONFIG_USERCONFIG": str(host / ".npmrc"),
                "NPM_CONFIG_GLOBALCONFIG": str(host / ".npmrc"),
                "NPM_CONFIG_CACHE": str(host / "cache"),
                "PLAYWRIGHT_BROWSERS_PATH": str(host / "browsers"),
            }
            for name, source, arguments in (("direct", probe, ()), ("nested", nested, (probe,))):
                with self.subTest(name=name):
                    payload = root / f"{name}.json"
                    with (
                        patch.dict(os.environ, environment),
                        patch.object(foundation_verifier, "_NPM_CACHE", caches[0]),
                        patch.object(foundation_verifier, "_PLAYWRIGHT_CACHE", caches[1]),
                    ):
                        foundation_verifier._run(
                            (sys.executable, "-c", source, str(payload), *arguments),
                            cwd=foundation_verifier.PLATFORM_REPOSITORY, timeout=30,
                        )
                    data = json.loads(payload.read_text())
                    env = data["environment"]
                    self.assertNotIn("PRIVATE_TOKEN", json.dumps(data))
                    self.assertNotIn(str(host), json.dumps(data))
                    self.assertEqual(data["homeMode"], 0o700)
                    self.assertEqual(data["configsReadable"], [False] * 3)
                    self.assertEqual(data["cacheTargets"], [str(cache) for cache in caches])
                    self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
                    self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
                    self.assertFalse(Path(env["HOME"]).exists())
            for cache in caches:
                self.assertEqual((cache / "sentinel").read_text(), "keep")
            unsafe = root / "linked-cache"
            unsafe.symlink_to(caches[0], target_is_directory=True)
            for invalid in (root / "missing", Path("relative"), unsafe):
                with (
                    self.subTest(cache=invalid),
                    patch.object(foundation_verifier, "_NPM_CACHE", invalid),
                    patch.object(foundation_verifier, "_PLAYWRIGHT_CACHE", caches[1]),
                    patch.object(foundation_verifier.subprocess, "Popen") as spawn,
                    self.assertRaises(RuntimeError),
                ):
                    foundation_verifier._run((sys.executable, "-c", "pass"), cwd=root, timeout=1)
                spawn.assert_not_called()

    @acceptance
    def test_bounded_child_failures_reap_process_groups_and_remove_private_homes(self):
        probe = """
import json, os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[1]).write_text(json.dumps([os.getpgrp(), os.environ['HOME']]))
if sys.argv[2] == 'overflow':
    os.write(1, b'PRIVATE_OUTPUT' * 1000)
if sys.argv[2] == 'nonzero':
    raise SystemExit(7)
time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            for outcome in ("timeout", "overflow", "nonzero"):
                with self.subTest(outcome=outcome):
                    evidence = root / f"{outcome}.json"
                    with (
                        patch.object(foundation_verifier, "_NPM_CACHE", cache),
                        patch.object(foundation_verifier, "_PLAYWRIGHT_CACHE", cache),
                        patch.object(foundation_verifier, "_MAX_CAPTURE_BYTES", 1024),
                        self.assertRaises(RuntimeError) as caught,
                    ):
                        foundation_verifier._run(
                            (sys.executable, "-c", probe, str(evidence), outcome),
                            cwd=root, timeout=0.3 if outcome == "timeout" else 5,
                            capture=outcome == "overflow",
                        )
                    group, home = json.loads(evidence.read_text())
                    self.assertFalse(foundation_verifier._process_group_exists(group))
                    self.assertFalse(Path(home).exists())
                    self.assertNotIn("PRIVATE_OUTPUT", str(caught.exception))
                    self.assertNotIn(str(root), str(caught.exception))

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


if __name__ == "__main__":
    unittest.main()
