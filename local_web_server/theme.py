"""Validation, descriptor-confined activation, and rollback for the live theme."""

import fcntl
import hashlib
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .colour import contrast_ratio, derive_accessible_accent


THEME_ROUTE = "/_local-web/platform/theme.css"
CURRENT_THEME_NAME = "theme.css"
PREVIOUS_THEME_NAME = "theme.previous.css"
_LOCK_NAME = ".theme.lock"

_ROOT = ":root"
_LIGHT = ':root[data-lwp-colour-mode="light"]'
_DARK = ':root[data-lwp-colour-mode="dark"]'
_SELECTORS = (_ROOT, _LIGHT, _DARK)
_BASE_TOKENS = {
    "--lwp-font-sans",
    "--lwp-font-mono",
    "--lwp-space-1",
    "--lwp-space-2",
    "--lwp-space-3",
    "--lwp-space-4",
    "--lwp-space-6",
    "--lwp-space-8",
    "--lwp-radius-control",
    "--lwp-radius-surface",
    "--lwp-border-width",
    "--lwp-focus-width",
    "--lwp-text-size-compact",
    "--lwp-motion-duration-fast",
    "--lwp-motion-easing-standard",
}
_SEMANTIC_TOKENS = {
    "--lwp-colour-canvas",
    "--lwp-colour-surface",
    "--lwp-colour-text",
    "--lwp-colour-text-muted",
    "--lwp-colour-line",
    "--lwp-colour-accent",
    "--lwp-colour-accent-contrast",
    "--lwp-colour-danger",
    "--lwp-colour-warning",
    "--lwp-colour-success",
    "--lwp-colour-focus",
    "--lwp-shadow",
}
_ALLOWED_DECLARATIONS = _BASE_TOKENS | _SEMANTIC_TOKENS | {"color-scheme"}
_HEX = re.compile(r"^#[0-9A-F]{6}$")
_BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DIMENSION = re.compile(r"^(?:0|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:px|rem))$")
_DURATION = re.compile(r"^(?:0|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:ms|s))$")
_EASING_KEYWORDS = {"linear", "ease", "ease-in", "ease-out", "ease-in-out"}
_NUMBER = r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_BORDER_WIDTH = re.compile(rf"^(?P<number>{_NUMBER})(?P<unit>px|rem)$")
_BORDER_WIDTH_LIMITS = {
    "px": (Decimal("1"), Decimal("4")),
    "rem": (Decimal(".0625"), Decimal(".25")),
}
_CUBIC_BEZIER = re.compile(
    rf"^cubic-bezier\((?P<x1>{_NUMBER}), (?P<y1>{_NUMBER}), "
    rf"(?P<x2>{_NUMBER}), (?P<y2>{_NUMBER})\)$"
)
_FONT = re.compile(r'^[A-Za-z0-9 ,."\'-]+$')
_RGBA = r"rgba\([0-9]{1,3}, [0-9]{1,3}, [0-9]{1,3}, (?:0|1|\.[0-9]+)\)"
_SHADOW = re.compile(
    rf"^(?P<prefix>(?:-?(?:0|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:px|rem)) ){{3}})"
    rf"(?P<colour>{_RGBA})$"
)


class ThemeError(ValueError):
    """A theme could not be validated or safely changed."""


def _invalid() -> ThemeError:
    return ThemeError("theme validation failed")


def _valid_easing(value: str) -> bool:
    if value in _EASING_KEYWORDS:
        return True
    match = _CUBIC_BEZIER.fullmatch(value)
    if match is None:
        return False
    x1 = Decimal(match.group("x1"))
    x2 = Decimal(match.group("x2"))
    return Decimal(0) <= x1 <= Decimal(1) and Decimal(0) <= x2 <= Decimal(1)


def _valid_border_width(value: str) -> bool:
    match = _BORDER_WIDTH.fullmatch(value)
    if match is None:
        return False
    minimum, maximum = _BORDER_WIDTH_LIMITS[match.group("unit")]
    width = Decimal(match.group("number"))
    return minimum <= width <= maximum


def _parse(content: bytes) -> dict[str, dict[str, str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid() from error
    if (
        "/*" in text
        or "*/" in text
        or "\\" in text
        or "@" in text
        or any(ord(character) < 32 and character not in "\n\r\t" for character in text)
    ):
        raise _invalid()
    declarations: dict[str, dict[str, str]] = {}
    cursor = 0
    for match in _BLOCK.finditer(text):
        if text[cursor : match.start()].strip():
            raise _invalid()
        selector = match.group(1).strip()
        if selector not in _SELECTORS or selector in declarations:
            raise _invalid()
        values: dict[str, str] = {}
        for raw in match.group(2).split(";"):
            raw = raw.strip()
            if not raw:
                continue
            if ":" not in raw:
                raise _invalid()
            name, value = (part.strip() for part in raw.split(":", 1))
            if name not in _ALLOWED_DECLARATIONS or name in values or not value:
                raise _invalid()
            values[name] = value
        declarations[selector] = values
        cursor = match.end()
    if text[cursor:].strip() or set(declarations) != set(_SELECTORS):
        raise _invalid()
    if set(declarations[_ROOT]) != _ALLOWED_DECLARATIONS:
        raise _invalid()
    for selector in (_LIGHT, _DARK):
        if set(declarations[selector]) != _SEMANTIC_TOKENS | {"color-scheme"}:
            raise _invalid()
    if declarations[_ROOT]["color-scheme"] != "light dark":
        raise _invalid()
    if declarations[_LIGHT]["color-scheme"] != "light":
        raise _invalid()
    if declarations[_DARK]["color-scheme"] != "dark":
        raise _invalid()
    _validate_value_grammar(declarations)
    return declarations


def _validate_value_grammar(declarations: dict[str, dict[str, str]]) -> None:
    root = declarations[_ROOT]
    for name in ("--lwp-font-sans", "--lwp-font-mono"):
        if not _FONT.fullmatch(root[name]):
            raise _invalid()
    non_dimension_tokens = {
        "--lwp-font-sans",
        "--lwp-font-mono",
        "--lwp-border-width",
        "--lwp-motion-duration-fast",
        "--lwp-motion-easing-standard",
    }
    for name in _BASE_TOKENS - non_dimension_tokens:
        if not _DIMENSION.fullmatch(root[name]):
            raise _invalid()
    if not _valid_border_width(root["--lwp-border-width"]):
        raise _invalid()
    if not _DURATION.fullmatch(root["--lwp-motion-duration-fast"]):
        raise _invalid()
    if not _valid_easing(root["--lwp-motion-easing-standard"]):
        raise _invalid()

    light = declarations[_LIGHT]
    dark = declarations[_DARK]
    for name in _SEMANTIC_TOKENS - {"--lwp-shadow"}:
        if not _HEX.fullmatch(light[name]) or not _HEX.fullmatch(dark[name]):
            raise _invalid()
        if root[name] != f"light-dark({light[name]}, {dark[name]})":
            raise _invalid()

    light_shadow = _SHADOW.fullmatch(light["--lwp-shadow"])
    dark_shadow = _SHADOW.fullmatch(dark["--lwp-shadow"])
    if light_shadow is None or dark_shadow is None:
        raise _invalid()
    if light_shadow.group("prefix") != dark_shadow.group("prefix"):
        raise _invalid()
    expected_shadow = (
        light_shadow.group("prefix")
        + "light-dark("
        + light_shadow.group("colour")
        + ", "
        + dark_shadow.group("colour")
        + ")"
    )
    if root["--lwp-shadow"] != expected_shadow:
        raise _invalid()


def _validate_contrast(
    declarations: dict[str, dict[str, str]], accents: tuple[str, ...]
) -> None:
    for selector in (_LIGHT, _DARK):
        colours = {
            name: value
            for name, value in declarations[selector].items()
            if name.startswith("--lwp-colour-")
        }
        backgrounds = (
            colours["--lwp-colour-canvas"],
            colours["--lwp-colour-surface"],
        )
        for name in ("--lwp-colour-text", "--lwp-colour-text-muted"):
            if any(contrast_ratio(colours[name], background) < 4.5 for background in backgrounds):
                raise _invalid()
        if contrast_ratio(
            colours["--lwp-colour-accent-contrast"], colours["--lwp-colour-accent"]
        ) < 4.5:
            raise _invalid()
        for name in (
            "--lwp-colour-line",
            "--lwp-colour-danger",
            "--lwp-colour-warning",
            "--lwp-colour-success",
            "--lwp-colour-focus",
        ):
            if contrast_ratio(colours[name], colours["--lwp-colour-surface"]) < 3.0:
                raise _invalid()
        for seed in accents:
            try:
                derived = derive_accessible_accent(seed, colours["--lwp-colour-surface"])
            except ValueError as error:
                raise _invalid() from error
            if contrast_ratio(derived, colours["--lwp-colour-surface"]) < 3.0:
                raise _invalid()


def validate_theme(content: bytes, accents: tuple[str, ...] = ()) -> str:
    """Validate the complete cosmetic contract and return its SHA-256 digest."""
    if not isinstance(content, bytes) or not content or not isinstance(accents, tuple):
        raise _invalid()
    declarations = _parse(content)
    _validate_contrast(declarations, accents)
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class _FileState:
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class ThemeSnapshot:
    current: _FileState
    previous: _FileState


def _atomic_write_at(directory_fd: int, name: str, content: bytes, mode: int) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        temporary = ""
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


class _ThemeSession:
    def __init__(
        self,
        directory_fd: int,
        chain: tuple[tuple[int, str, int], ...],
    ):
        self.directory_fd = directory_fd
        self.chain = chain

    def verify(self) -> None:
        try:
            for parent_fd, name, child_fd in self.chain:
                path_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                pinned_metadata = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(path_metadata.st_mode)
                    or (path_metadata.st_dev, path_metadata.st_ino)
                    != (pinned_metadata.st_dev, pinned_metadata.st_ino)
                ):
                    raise ThemeError("managed theme directory is unsafe")
        except ThemeError:
            raise
        except OSError as error:
            raise ThemeError("managed theme directory is unsafe") from error

    def read(self, name: str, *, required: bool = False) -> _FileState:
        self.verify()
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            if required:
                raise ThemeError("theme rollback unavailable")
            return _FileState(None, None)
        except OSError as error:
            raise ThemeError("managed theme file is unsafe") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ThemeError("managed theme file is unsafe")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                return _FileState(source.read(), stat.S_IMODE(metadata.st_mode))
        except ThemeError:
            raise
        except OSError as error:
            raise ThemeError("cannot inspect managed theme file") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def snapshot(self) -> ThemeSnapshot:
        return ThemeSnapshot(self.read(CURRENT_THEME_NAME), self.read(PREVIOUS_THEME_NAME))

    def write(self, name: str, state: _FileState, *, verify: bool = True) -> None:
        if state.content is None or state.mode is None:
            raise ThemeError("invalid managed theme state")
        if verify:
            self.verify()
        _atomic_write_at(self.directory_fd, name, state.content, state.mode)
        if verify:
            self.verify()

    def remove(self, name: str, *, verify: bool = True) -> None:
        if verify:
            self.read(name)
        try:
            os.unlink(name, dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        except FileNotFoundError:
            pass

    def restore(self, snapshot: ThemeSnapshot, *, verify: bool = True) -> None:
        try:
            for name, state in (
                (CURRENT_THEME_NAME, snapshot.current),
                (PREVIOUS_THEME_NAME, snapshot.previous),
            ):
                if state.content is None:
                    self.remove(name, verify=verify)
                else:
                    self.write(name, state, verify=verify)
        except Exception as error:
            raise ThemeError("theme recovery failed") from error

    def transition(self, desired: ThemeSnapshot) -> None:
        before = self.snapshot()
        try:
            for name, state in (
                (PREVIOUS_THEME_NAME, desired.previous),
                (CURRENT_THEME_NAME, desired.current),
            ):
                if state.content is None:
                    self.remove(name)
                else:
                    self.write(name, state)
        except Exception as error:
            try:
                self.restore(before, verify=False)
            except ThemeError as recovery_error:
                raise ThemeError("theme transition failed; recovery failed") from recovery_error
            raise ThemeError("theme transition failed") from error


class ThemeStore:
    """Own the current and immediately previous validated platform themes."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.platform_root = self.runtime_root / "platform"
        self.current = self.platform_root / CURRENT_THEME_NAME
        self.previous = self.platform_root / PREVIOUS_THEME_NAME
        self._local = threading.local()

    def _active_session(self) -> _ThemeSession | None:
        return getattr(self._local, "session", None)

    @staticmethod
    def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise ThemeError("cannot prepare managed theme directory")
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                return os.open(name, flags, dir_fd=parent_fd)
            except OSError as error:
                raise ThemeError("cannot prepare managed theme directory") from error
        except OSError as error:
            raise ThemeError("managed theme directory is unsafe") from error

    def _open_platform(
        self,
    ) -> tuple[tuple[int, ...], tuple[tuple[int, str, int], ...]]:
        if not self.runtime_root.is_absolute():
            raise ThemeError("managed theme directory is unsafe")
        descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
        chain: list[tuple[int, str, int]] = []
        try:
            parts = self.runtime_root.parts[1:]
            for index, part in enumerate(parts):
                following = self._open_directory(
                    descriptors[-1], part, create=index == len(parts) - 1
                )
                chain.append((descriptors[-1], part, following))
                descriptors.append(following)
            platform = self._open_directory(descriptors[-1], "platform", create=True)
            chain.append((descriptors[-1], "platform", platform))
            descriptors.append(platform)
            try:
                os.fchmod(descriptors[-2], 0o700)
                os.fchmod(platform, 0o700)
            except OSError as error:
                raise ThemeError("cannot prepare managed theme directory") from error
            return tuple(descriptors), tuple(chain)
        except Exception:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @contextmanager
    def transaction(self) -> Iterator["ThemeStore"]:
        if self._active_session() is not None:
            yield self
            return
        descriptors, chain = self._open_platform()
        platform_fd = descriptors[-1]
        lock_fd = -1
        try:
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=platform_fd,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ThemeError("managed theme lock is unsafe")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            session = _ThemeSession(platform_fd, chain)
            self._local.session = session
            session.verify()
            yield self
            session.verify()
        except ThemeError:
            raise
        except OSError as error:
            raise ThemeError("cannot lock managed theme") from error
        finally:
            self._local.session = None
            if lock_fd != -1:
                os.close(lock_fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _session(self) -> _ThemeSession:
        session = self._active_session()
        if session is None:
            raise RuntimeError("theme transaction is not active")
        return session

    def snapshot(self) -> ThemeSnapshot:
        if self._active_session() is not None:
            return self._session().snapshot()
        with self.transaction():
            return self._session().snapshot()

    def restore(self, snapshot: ThemeSnapshot) -> None:
        if self._active_session() is not None:
            self._session().restore(snapshot)
            return
        with self.transaction():
            self._session().restore(snapshot)

    def activate(self, content: bytes) -> str:
        digest = validate_theme(content)
        if self._active_session() is None:
            with self.transaction():
                return self.activate(content)
        session = self._session()
        before = session.snapshot()
        if before.current.content == content:
            return digest
        if before.current.content is not None:
            validate_theme(before.current.content)
        if before.previous.content is not None:
            validate_theme(before.previous.content)
        previous = (
            _FileState(before.current.content, 0o600)
            if before.current.content is not None
            else before.previous
        )
        session.transition(
            ThemeSnapshot(_FileState(content, 0o600), previous)
        )
        return digest

    def rollback(self) -> str:
        if self._active_session() is None:
            with self.transaction():
                return self.rollback()
        session = self._session()
        before = session.snapshot()
        if before.current.content is None or before.previous.content is None:
            raise ThemeError("theme rollback unavailable")
        validate_theme(before.current.content)
        digest = validate_theme(before.previous.content)
        session.transition(
            ThemeSnapshot(
                _FileState(before.previous.content, 0o600),
                _FileState(before.current.content, 0o600),
            )
        )
        return digest
