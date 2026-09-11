"""Validation of the built frontend declared by an application manifest."""

from __future__ import annotations

import stat
import os
from html.parser import HTMLParser
from pathlib import Path

from .models import AppManifest


_THEME_ROUTE = "/_local-web/platform/theme.css"
_MAX_ENTRY_BYTES = 2 * 1024 * 1024


class ReleaseOutcomeError(ValueError):
    """The declared release does not contain a valid platform frontend."""


class _ThemeLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.invalid = False
        self.matches = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "link":
            return
        names = [name.lower() for name, _value in attrs]
        if len(names) != len(set(names)):
            self.invalid = True
            return
        values = {name.lower(): value for name, value in attrs if value is not None}
        rel = {item.lower() for item in values.get("rel", "").split()}
        if (
            "stylesheet" in rel
            and values.get("href") == _THEME_ROUTE
        ):
            self.matches += 1


def _require_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError


def _walk_directories(root: Path, relative: Path) -> Path:
    current = root
    _require_directory(current)
    for component in relative.parts:
        if component in ("", ".", ".."):
            raise ValueError
        current /= component
        _require_directory(current)
    return current


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def validate_release_outcome(
    repository: Path, manifest: AppManifest, release: Path
) -> None:
    """Require the declared release to expose one bounded platform-themed entry."""

    try:
        root = Path(repository).resolve(strict=True)
        _require_directory(root)
        expected_release = root / manifest.build.output
        reported_release = _lexical_absolute(Path(release))
        try:
            reported_release = root / reported_release.relative_to(
                _lexical_absolute(Path(repository))
            )
        except ValueError:
            pass
        if reported_release != expected_release:
            raise ValueError
        release_directory = _walk_directories(
            root, expected_release.relative_to(root)
        )

        frontend_output = None
        if manifest.kind == "service" and manifest.service is not None:
            frontend_output = manifest.service.frontend_output
        frontend_directory = (
            _walk_directories(release_directory, frontend_output)
            if frontend_output is not None
            else release_directory
        )
        entry = frontend_directory / "index.html"
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError
        with entry.open("rb") as stream:
            contents = stream.read(_MAX_ENTRY_BYTES + 1)
        if len(contents) > _MAX_ENTRY_BYTES:
            raise ValueError
        parser = _ThemeLinks()
        parser.feed(contents.decode("utf-8"))
        parser.close()
        if parser.invalid or parser.matches != 1:
            raise ValueError
    except Exception as error:
        raise ReleaseOutcomeError("application release outcome is invalid") from error
