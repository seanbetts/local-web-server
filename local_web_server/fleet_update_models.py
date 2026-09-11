"""Immutable plans for a read-only registered application platform refresh."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .app_update_models import AppUpdatePlan
from .app_activation import AppActivationPlan


FleetPreviewStatus = Literal["CURRENT", "READY", "SKIPPED", "BLOCKED"]
FleetApplyStatus = Literal[
    "UPDATED",
    "CURRENT",
    "SKIPPED",
    "BLOCKED",
    "FAILED_RECOVERED",
    "RECOVERY_FAILED",
]


@dataclass(frozen=True)
class FleetAppPlan:
    app_id: str
    status: FleetPreviewStatus
    current_ui_version: str | None
    target_ui_version: str | None
    target_ui_sha256: str | None
    source_head: str | None
    deployed_commit: str | None
    paths: tuple[Path, ...]
    reason: str | None
    repository: Path = field(repr=False, compare=False)
    _update_plan: AppUpdatePlan | None = field(repr=False, compare=False)
    _activation_plan: AppActivationPlan | None = field(
        repr=False, compare=False, default=None
    )
    _runtime_root: Path | None = field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class FleetUpdatePlan:
    apps: tuple[FleetAppPlan, ...]


@dataclass(frozen=True)
class FleetAppResult:
    app_id: str
    status: FleetApplyStatus
    current_ui_version: str | None
    target_ui_version: str | None
    source_head: str | None
    deployed_commit: str | None
    paths: tuple[Path, ...]
    verified: bool
    reason: str | None


@dataclass(frozen=True)
class FleetUpdateResult:
    apps: tuple[FleetAppResult, ...]
