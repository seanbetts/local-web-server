"""Composition of structural and app-local verification."""

from __future__ import annotations

from pathlib import Path

from .app_doctor import AppDoctor, AppInspector, DoctorReport
from .config import ConfigError, load_manifest
from .process_runner import (
    ProcessCommand,
    ProcessRunError,
    ProcessRunner,
    ProcessWorkflowRunner,
)
from .release_composer import (
    AppReleaseComposer,
    ReleaseComposer,
    ReleaseCompositionError,
)
from .release_outcome import ReleaseOutcomeError, validate_release_outcome


_APP_COMMANDS = (
    ProcessCommand("npm-check", ("npm", "run", "check")),
    ProcessCommand("npm-e2e", ("npm", "run", "test:e2e")),
)


class AppCheckError(ValueError):
    """A composed application check did not complete successfully."""


class AppChecker:
    def __init__(
        self,
        *,
        doctor: AppInspector | None = None,
        process_runner: ProcessWorkflowRunner | None = None,
        release_composer: AppReleaseComposer | None = None,
    ) -> None:
        self._doctor = doctor if doctor is not None else AppDoctor()
        self._process_runner = (
            process_runner if process_runner is not None else ProcessRunner()
        )
        self._release_composer = (
            release_composer if release_composer is not None else ReleaseComposer()
        )

    def check(self, repository: Path) -> DoctorReport:
        report = self._doctor.inspect(repository)
        if report.diagnostics:
            raise AppCheckError("app has compatibility diagnostics")
        try:
            self._process_runner.run(repository, _APP_COMMANDS)
        except ProcessRunError as error:
            raise AppCheckError("app checks failed") from error
        try:
            manifest = load_manifest(Path(repository) / "local-web.json")
            release = self._release_composer.compose(repository, manifest.build)
            validate_release_outcome(Path(repository), manifest, release)
        except (
            ConfigError,
            ReleaseCompositionError,
            ReleaseOutcomeError,
            OSError,
        ) as error:
            raise AppCheckError("app release contract failed") from error
        return report
