import os
import tempfile
import unittest
from pathlib import Path

from local_web_server.agent_skill import AgentSkillInstallError, AgentSkillInstaller




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
