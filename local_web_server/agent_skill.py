"""Install the repository-owned Codex skill at its single supported target."""

import os
from dataclasses import dataclass
from pathlib import Path


SKILL_NAME = "local-web-app-development"


class AgentSkillInstallError(ValueError):
    """A privacy-safe failure to install the canonical agent skill."""


@dataclass(frozen=True)
class AgentSkillInstallResult:
    target: Path
    changed: bool
    dry_run: bool


class AgentSkillInstaller:
    def __init__(self, repository: Path, home: Path | None = None):
        self.repository = Path(os.path.abspath(repository))
        self.home = Path.home() if home is None else Path(home)
        self.source = self.repository / "skills" / SKILL_NAME
        self.target = self.home / ".codex" / "skills" / SKILL_NAME

    def install(self, dry_run: bool = False) -> AgentSkillInstallResult:
        try:
            if (
                self.source.is_symlink()
                or not self.source.is_dir()
                or not (self.source / "SKILL.md").is_file()
            ):
                raise AgentSkillInstallError("canonical agent skill is missing")

            self._validate_parent_chain()
            changed = self._change_required()
            if changed and not dry_run:
                self.target.parent.mkdir(parents=True, exist_ok=True)
                if self.target.is_symlink():
                    self.target.unlink()
                relative_source = os.path.relpath(self.source, self.target.parent)
                self.target.symlink_to(relative_source, target_is_directory=True)
        except AgentSkillInstallError:
            raise
        except OSError as error:
            raise AgentSkillInstallError("agent skill installation failed") from error

        return AgentSkillInstallResult(
            target=self.target,
            changed=changed,
            dry_run=dry_run,
        )

    def _validate_parent_chain(self) -> None:
        for parent in (self.home / ".codex", self.home / ".codex" / "skills"):
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise AgentSkillInstallError("refusing unsafe skill parent")

    def _change_required(self) -> bool:
        if self.target.is_symlink():
            lexical_target = self._lexical_link_target()
            if lexical_target == self.source:
                return False
            if lexical_target.is_relative_to(self.repository):
                return True
            raise AgentSkillInstallError("refusing to replace unrelated skill target")
        if self.target.exists():
            raise AgentSkillInstallError("refusing to replace unrelated skill target")
        return True

    def _lexical_link_target(self) -> Path:
        raw_target = Path(os.readlink(self.target))
        candidate = raw_target if raw_target.is_absolute() else self.target.parent / raw_target
        return Path(os.path.abspath(candidate))
