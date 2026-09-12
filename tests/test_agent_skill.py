import os
import tempfile
import unittest
from pathlib import Path

from local_web_server.agent_skill import AgentSkillInstallError, AgentSkillInstaller


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "local-web-app-development"


class AgentSkillContentTests(unittest.TestCase):
    def read_skill(self) -> str:
        return (SKILL / "SKILL.md").read_text(encoding="utf-8")

    def test_metadata_triggers_every_supported_intent(self):
        skill = self.read_skill()
        frontmatter = skill.split("---", 2)[1]

        self.assertIn("name: local-web-app-development", frontmatter)
        for trigger in ("new", "existing", "diagnos", "verif", "activat"):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, frontmatter.lower())
        self.assertNotIn("upgrade", frontmatter.lower())

    def test_skill_deterministically_resolves_and_verifies_the_platform_cli(self):
        """A fresh shell without local-web on PATH must have one prescribed fallback."""
        skill = self.read_skill()

        self.assertIn("command -v local-web", skill)
        self.assertIn("Path.home()", skill)
        self.assertIn(".codex/skills/local-web-app-development", skill)
        self.assertIn(".resolve(strict=True)", skill)
        self.assertIn('"bin/local-web"', skill)
        self.assertIn('"$LOCAL_WEB" --help', skill)
        self.assertNotIn("$HOME", skill)

    def test_existing_apps_preserve_app_owned_structure(self):
        contract = (SKILL / "references" / "app-contract.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Read `AGENTS.md` and `local-web.json` before editing", contract)
        self.assertIn("Preserve the app's domain-owned structure", contract)
        self.assertIn("Domain code remains app-owned", contract)

    def test_routine_fleet_refresh_is_distinct_from_app_owned_updates(self):
        """Fleet refreshes require a preview and separate approval before apply."""
        skill = self.read_skill()
        contract = (SKILL / "references" / "app-contract.md").read_text(
            encoding="utf-8"
        )
        verification = (SKILL / "references" / "verification.md").read_text(
            encoding="utf-8"
        )
        guidance = "\n".join((skill, contract, verification))

        dry_run = guidance.index('"$LOCAL_WEB" apps update --dry-run')
        review = guidance.index("Review every final app status", dry_run)
        approval = guidance.index("separate explicit approval", review)
        apply = guidance.index('"$LOCAL_WEB" apps update --apply', approval)
        self.assertLess(dry_run, review)
        self.assertLess(review, approval)
        self.assertLess(approval, apply)
        for required in (
            "focused app commit",
            "activation",
            "legacy adoption",
            "foundation adoption",
            "domain migration",
            "single-app route",
            "SKIPPED",
            "live commit",
            "source commit",
            "deployed commit",
            "live-release-not-current",
            "app activate",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guidance)

    def test_existing_service_foundation_adoption_orders_each_verification_gate(self):
        contract = (SKILL / "references" / "app-contract.md").read_text(
            encoding="utf-8"
        )
        preview = contract.index(
            '"$LOCAL_WEB" app update --repository . --foundation react-vite --dry-run'
        )
        apply = contract.index(
            '"$LOCAL_WEB" app update --repository . --foundation react-vite',
            preview
            + len(
                '"$LOCAL_WEB" app update --repository . --foundation react-vite --dry-run'
            ),
        )
        focused_diff = contract.index("git diff -- local-web.json", apply)
        doctor = contract.index(
            '"$LOCAL_WEB" app doctor --repository .', focused_diff
        )
        local_check = contract.index("npm run check", doctor)
        browser = contract.index("npm run test:e2e", local_check)
        full_check = contract.index(
            '"$LOCAL_WEB" app check --repository .', browser
        )

        self.assertLess(preview, apply)
        self.assertLess(apply, focused_diff)
        self.assertLess(focused_diff, doctor)
        self.assertLess(doctor, local_check)
        self.assertLess(local_check, browser)
        self.assertLess(browser, full_check)

    def test_skill_uses_platform_commands_without_copying_platform_implementation(self):
        contents = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL / "SKILL.md", *(SKILL / "references").glob("*.md"))
        )

        for command in (
            '"$LOCAL_WEB" app init',
            '"$LOCAL_WEB" app doctor',
            '"$LOCAL_WEB" app check',
            '"$LOCAL_WEB" app identity',
            '"$LOCAL_WEB" app activate',
        ):
            with self.subTest(command=command):
                self.assertIn(command, contents)
        for forbidden in (
            "templates/react-app",
            "local_web_server.app_",
            "init_skill.py",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, contents)

    def test_verification_keeps_app_checks_local_and_activates_only_after_health(self):
        verification = (SKILL / "references" / "verification.md").read_text(
            encoding="utf-8"
        )

        app_check = verification.index("`npm run check`")
        e2e = verification.index("`npm run test:e2e`")
        platform_check = verification.index(
            '`"$LOCAL_WEB" app check --repository .`'
        )
        activate = verification.index(
            '`"$LOCAL_WEB" app activate --repository .`'
        )
        apply = verification.index(
            '`"$LOCAL_WEB" app activate --repository . --apply`'
        )
        self.assertLess(app_check, activate)
        self.assertLess(e2e, activate)
        self.assertLess(platform_check, activate)
        self.assertLess(activate, apply)
        self.assertIn("app-local", verification)
        self.assertIn("must not invoke `local-web`", verification)
        self.assertIn("healthy", verification[platform_check:activate].lower())

    def test_skill_contains_only_the_three_planned_references(self):
        references = sorted(path.name for path in (SKILL / "references").iterdir())

        self.assertEqual(
            references,
            ["app-contract.md", "service-apps.md", "verification.md"],
        )

    def test_skill_routes_supported_services_without_bypassing_platform_gates(self):
        skill = self.read_skill()
        service = (SKILL / "references" / "service-apps.md").read_text(encoding="utf-8")
        verification = (SKILL / "references" / "verification.md").read_text(encoding="utf-8")

        self.assertIn("--kind service", skill)
        self.assertNotIn("--port <port>", skill)
        self.assertNotIn("--port <port>", service)
        self.assertIn("assigned automatically", service)
        self.assertIn("build.release", service)
        self.assertIn("127.0.0.1", service)
        self.assertIn("must not invoke `local-web`", service)
        for term in (
            "{repository}",
            "immutable release",
            "atomic writes",
            "backups",
            "app activate --repository .",
            "--apply",
        ):
            with self.subTest(term=term):
                self.assertIn(term, service)
        self.assertIn("platform capability blocker", skill)
        self.assertIn("approved concept", verification)
        self.assertIn("390 px", verification)
        self.assertIn("320 px", verification)

    def test_existing_service_command_migration_has_a_bounded_approved_workflow(self):
        service = (SKILL / "references" / "service-apps.md").read_text(
            encoding="utf-8"
        )
        verification = (SKILL / "references" / "verification.md").read_text(
            encoding="utf-8"
        )
        guidance = "\n".join(
            (
                (ROOT / "README.md").read_text(encoding="utf-8"),
                self.read_skill(),
                service,
                verification,
            )
        )

        workflow = " ".join(service.split())
        commit = workflow.index("commit the app-owned manifest change")
        doctor = workflow.index('"$LOCAL_WEB" app doctor --repository .', commit)
        check = workflow.index('"$LOCAL_WEB" app check --repository .', doctor)
        preview = workflow.index(
            '"$LOCAL_WEB" app migrate-service-command --repository .', check
        )
        inspect = workflow.index("Inspect the bounded preview", preview)
        approval = workflow.index("obtain explicit approval", inspect)
        apply = workflow.index(
            '"$LOCAL_WEB" app migrate-service-command --repository . --apply',
            approval,
        )
        self.assertLess(commit, doctor)
        self.assertLess(doctor, check)
        self.assertLess(check, preview)
        self.assertLess(preview, inspect)
        self.assertLess(inspect, approval)
        self.assertLess(approval, apply)

        for boundary in (
            "ID, canonical repository, route, and port remain fixed",
            "target argv comes only from the committed manifest",
            "normal activate remains strict",
            "application-owned and retry-safe",
            "registry editing is forbidden",
            "separate explicit approval",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, " ".join(guidance.split()))

    def test_existing_service_public_base_path_migration_has_a_bounded_approved_workflow(self):
        app_contract = (SKILL / "references" / "app-contract.md").read_text(
            encoding="utf-8"
        )
        service = (SKILL / "references" / "service-apps.md").read_text(
            encoding="utf-8"
        )
        verification = (SKILL / "references" / "verification.md").read_text(
            encoding="utf-8"
        )
        guidance = "\n".join(
            (
                (ROOT / "README.md").read_text(encoding="utf-8"),
                self.read_skill(),
                app_contract,
                service,
                verification,
            )
        )

        workflow = " ".join(service.split())
        doctor = workflow.index('"$LOCAL_WEB" app doctor --repository .')
        check = workflow.index('"$LOCAL_WEB" app check --repository .', doctor)
        preview = workflow.index(
            '"$LOCAL_WEB" app migrate-public-base-path --repository .', check
        )
        inspect = workflow.index("Inspect the bounded preview", preview)
        approval = workflow.index("obtain explicit approval", inspect)
        apply = workflow.index(
            '"$LOCAL_WEB" app migrate-public-base-path --repository . --apply',
            approval,
        )
        self.assertLess(doctor, check)
        self.assertLess(check, preview)
        self.assertLess(preview, inspect)
        self.assertLess(inspect, approval)
        self.assertLess(approval, apply)

        for boundary in (
            "VITE_PUBLIC_BASE_PATH",
            "committed manifest route plus a trailing slash",
            "fresh immutable target release",
            "identity, canonical repository, route, port, and expanded command remain unchanged",
            "normal activation remains strict",
            "Registry editing is forbidden",
            "no application database authority",
            "separate explicit approval",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, " ".join(guidance.split()))

    def test_generated_agent_metadata_matches_the_skill(self):
        lines = (SKILL / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(lines[0], "interface:")
        metadata = {
            key.strip(): value.strip().strip('"')
            for line in lines[1:]
            for key, value in (line.split(":", 1),)
        }
        self.assertEqual(
            set(metadata),
            {"display_name", "short_description", "default_prompt"},
        )
        self.assertTrue(metadata["display_name"])
        self.assertTrue(metadata["short_description"])
        self.assertIn("$local-web-app-development", metadata["default_prompt"])


class AgentSkillInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "local-web-server"
        self.source = self.repository / "skills" / "local-web-app-development"
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text("skill\n", encoding="utf-8")
        self.home = self.root / "home"
        self.target = self.home / ".codex" / "skills" / "local-web-app-development"

    def tearDown(self):
        self.temp.cleanup()

    def installer(self) -> AgentSkillInstaller:
        return AgentSkillInstaller(repository=self.repository, home=self.home)

    def test_install_creates_the_owned_relative_link(self):
        result = self.installer().install(dry_run=False)

        self.assertEqual(result.target, self.target)
        self.assertTrue(result.changed)
        self.assertFalse(result.dry_run)
        self.assertTrue(self.target.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(self.target)))
        self.assertEqual(self.target.resolve(), self.source.resolve())

    def test_dry_run_reports_change_without_creating_parents(self):
        result = self.installer().install(dry_run=True)

        self.assertEqual(result.target, self.target)
        self.assertTrue(result.changed)
        self.assertTrue(result.dry_run)
        self.assertFalse(self.home.exists())

    def test_missing_source_is_refused_without_creating_target(self):
        (self.source / "SKILL.md").unlink()
        self.source.rmdir()

        with self.assertRaisesRegex(
            AgentSkillInstallError, "canonical agent skill is missing"
        ):
            self.installer().install()

        self.assertFalse(self.home.exists())

    def test_correct_link_is_an_idempotent_no_op(self):
        self.installer().install()

        result = self.installer().install()

        self.assertFalse(result.changed)
        self.assertEqual(self.target.resolve(), self.source.resolve())

    def test_repairs_only_a_stale_link_with_a_lexical_target_inside_repository(self):
        self.target.parent.mkdir(parents=True)
        stale_source = self.repository / "skills" / "retired-local-web-skill"
        self.target.symlink_to(os.path.relpath(stale_source, self.target.parent))

        result = self.installer().install()

        self.assertTrue(result.changed)
        self.assertEqual(self.target.resolve(), self.source.resolve())

    def test_refuses_regular_file_directory_and_unrelated_link(self):
        cases = ("file", "directory", "unrelated-link")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_home:
                    home = Path(temporary_home)
                    target = (
                        home / ".codex" / "skills" / "local-web-app-development"
                    )
                    target.parent.mkdir(parents=True)
                    if case == "file":
                        target.write_text("preserve\n", encoding="utf-8")
                    elif case == "directory":
                        target.mkdir()
                    else:
                        target.symlink_to(self.root / "unrelated-skill")

                    with self.assertRaisesRegex(
                        AgentSkillInstallError, "refusing to replace unrelated skill target"
                    ):
                        AgentSkillInstaller(
                            repository=self.repository, home=home
                        ).install()

                    if case == "file":
                        self.assertEqual(target.read_text(encoding="utf-8"), "preserve\n")
                    elif case == "directory":
                        self.assertTrue(target.is_dir())
                    else:
                        self.assertEqual(os.readlink(target), str(self.root / "unrelated-skill"))


if __name__ == "__main__":
    unittest.main()
