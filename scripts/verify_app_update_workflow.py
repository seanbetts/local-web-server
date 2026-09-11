#!/usr/bin/env python3
"""Verify legacy application adoption without touching a real application."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server import cli
from local_web_server.app_update import AppUpdater, NpmLockfileBuilder
from local_web_server.process_runner import ProcessRunError


EXPECTED_UPDATE_PATHS = frozenset(
    {
        Path(".local-web-platform.json"),
        Path("index.html"),
        Path("local-web.json"),
        Path("package-lock.json"),
        Path("package.json"),
        Path("vendor/local-web-ui.tgz"),
    }
)


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class WorkflowFailure(RuntimeError):
    """The disposable adoption workflow did not meet its contract."""


class _FailingNpmRunner:
    def run(self, _repository, _commands) -> None:
        raise ProcessRunError("injected npm failure")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _run(
    argv: tuple[str, ...],
    repository: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=600,
    )


def _require_success(argv: tuple[str, ...], repository: Path) -> None:
    if _run(argv, repository).returncode != 0:
        raise WorkflowFailure("fixture Git operation failed")


def _write_fixture(repository: Path) -> None:
    repository.mkdir()
    manifest = {
        "schemaVersion": 1,
        "id": "legacy-adoption",
        "title": "Legacy Adoption",
        "route": "/legacy-adoption",
        "kind": "static",
        "build": {
            "commands": [["npm", "run", "build"]],
            "output": "dist",
            "environment": [],
        },
        "healthPath": "/legacy-adoption/",
        "home": {"icon": "apps", "accent": "#D9467A"},
    }
    package = {
        "name": "legacy-adoption",
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "dev": "vite",
            "lint": "eslint .",
            "test": "vitest run",
            "build": "vite build",
            "test:e2e": "playwright test",
        },
        "dependencies": {},
        "devDependencies": {},
    }
    lockfile = {
        "name": "legacy-adoption",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "legacy-adoption",
                "version": "1.0.0",
                "dependencies": {},
                "devDependencies": {},
            }
        },
    }
    files = {
        Path("local-web.json"): _json_bytes(manifest),
        Path("package.json"): _json_bytes(package),
        Path("package-lock.json"): _json_bytes(lockfile),
        Path("index.html"): (
            b"<!doctype html>\n<html><head><title>Legacy Adoption</title></head>"
            b'<body><div id="root"></div><script type="module" '
            b'src="/src/main.tsx"></script></body></html>\n'
        ),
        Path("src/main.tsx"): (
            b'import React from "react";\n'
            b'import { createRoot } from "react-dom/client";\n'
            b'createRoot(document.getElementById("root")!).render('
            b"<React.StrictMode />);\n"
        ),
        Path("vite.config.ts"): (
            b'import { defineConfig } from "vite";\n'
            b"export default defineConfig({ base: '/legacy-adoption/' });\n"
        ),
    }
    for relative, content in files.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    _require_success(("git", "init", "--initial-branch=main"), repository)
    _require_success(("git", "add", "--all"), repository)
    _require_success(
        (
            "git",
            "-c",
            "user.name=Local Web Verification",
            "-c",
            "user.email=local-web-verification@localhost",
            "commit",
            "-m",
            "Legacy fixture",
        ),
        repository,
    )
    (repository / "private-notes.md").write_text("private", encoding="utf-8")


def _snapshot(repository: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }


def _target_snapshot(repository: Path) -> dict[Path, bytes | None]:
    return {
        relative: (
            (repository / relative).read_bytes()
            if (repository / relative).is_file()
            else None
        )
        for relative in EXPECTED_UPDATE_PATHS
    }


def _changed_paths(
    before: dict[Path, bytes], after: dict[Path, bytes]
) -> frozenset[Path]:
    return frozenset(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _run_cli(*arguments: str) -> CliResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        returncode = cli.main(arguments)
    return CliResult(returncode, stdout.getvalue(), stderr.getvalue())


def _commit_paths(repository: Path) -> None:
    ordered = tuple(sorted(path.as_posix() for path in EXPECTED_UPDATE_PATHS))
    _require_success(("git", "add", "--", *ordered), repository)
    _require_success(
        (
            "git",
            "-c",
            "user.name=Local Web Verification",
            "-c",
            "user.email=local-web-verification@localhost",
            "commit",
            "-m",
            "Adopt platform contract",
            "--",
            *ordered,
        ),
        repository,
    )


def _verify_legacy_update(repository: Path) -> int:
    doctor = _run_cli("app", "doctor", "--repository", str(repository))
    if not doctor.stdout.startswith("WARNING platform.legacy:"):
        raise WorkflowFailure("legacy Doctor result was not reported")

    before = _snapshot(repository)
    preview = _run_cli(
        "app",
        "update",
        "--repository",
        str(repository),
        "--capability",
        "supabase",
        "--dry-run",
    )
    if preview.returncode != 0 or "mode: adopt" not in preview.stdout:
        raise WorkflowFailure("legacy update preview failed")
    if _snapshot(repository) != before:
        raise WorkflowFailure("legacy update preview wrote repository bytes")

    update = _run_cli(
        "app",
        "update",
        "--repository",
        str(repository),
        "--capability",
        "supabase",
    )
    if update.returncode != 0:
        raise WorkflowFailure("legacy update failed")
    if (repository / "private-notes.md").read_text(encoding="utf-8") != "private":
        raise WorkflowFailure("unrelated dirty content changed")
    changed = _changed_paths(before, _snapshot(repository))
    if changed != EXPECTED_UPDATE_PATHS:
        raise WorkflowFailure("legacy update changed an unexpected path")

    _commit_paths(repository)
    final_doctor = _run_cli("app", "doctor", "--repository", str(repository))
    if final_doctor.returncode != 0:
        raise WorkflowFailure("adopted application failed Doctor")
    return len(changed)


def _verify_failed_update_restoration(repository: Path) -> int:
    before = _target_snapshot(repository)
    failing_updater = AppUpdater(
        PLATFORM_REPOSITORY,
        lockfile_builder=NpmLockfileBuilder(_FailingNpmRunner()),
    )
    with patch.object(cli, "AppUpdater", return_value=failing_updater):
        result = _run_cli(
            "app",
            "update",
            "--repository",
            str(repository),
            "--capability",
            "supabase",
        )
    if result.returncode != 2:
        raise WorkflowFailure("injected npm failure did not fail the update")
    if _target_snapshot(repository) != before:
        raise WorkflowFailure("failed update changed target bytes")
    return len(before)


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="local-web-app-update-workflow-") as text:
            root = Path(text)
            legacy = root / "legacy-adoption"
            failed = root / "failed-adoption"
            _write_fixture(legacy)
            changed_count = _verify_legacy_update(legacy)
            _write_fixture(failed)
            restored_count = _verify_failed_update_restoration(failed)
    except Exception:
        print("existing app update workflow: failed")
        return 1

    print(f"updated paths: {changed_count}")
    print(f"restored targets: {restored_count}")
    print("legacy update workflow: passed")
    print("failed update restoration: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
