"""Immutable values shared by application update planning and publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


FoundationKind = Literal["react-vite"]


@dataclass(frozen=True)
class AppUpdateRequest:
    repository: Path
    capabilities: tuple[str, ...] = ()
    foundation: FoundationKind | None = None


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class AppUpdatePlan:
    app_id: str
    mode: Literal["adopt", "refresh", "current"]
    current_ui_version: str | None
    target_ui_version: str
    target_ui_sha256: str
    capabilities: tuple[str, ...]
    changes: tuple[FileChange, ...]
    foundation: FoundationKind | None = None
    current_platform_contract_version: int | None = None
    current_template_version: int | None = None
    target_platform_contract_version: int | None = None
    target_template_version: int | None = None


@dataclass(frozen=True)
class AppUpdateResult:
    plan: AppUpdatePlan
    recovered: bool
