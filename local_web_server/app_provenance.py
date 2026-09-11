"""Strict, immutable compatibility metadata for generated platform apps."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import SUPPORTED_PLATFORM_CONTRACT
from .models import PlatformSpec


PROVENANCE_FILENAME = ".local-web-platform.json"
SUPPORTED_PROVENANCE_SCHEMA = 1
CURRENT_TEMPLATE_VERSION = 3

_ROOT_KEYS = frozenset(
    {
        "schemaVersion",
        "templateVersion",
        "platformContractVersion",
        "ui",
        "capabilities",
        "domainPaletteTokens",
        "managedFiles",
    }
)
_UI_KEYS = frozenset({"version", "sha256"})
_SUPPORTED_CAPABILITIES = frozenset({"supabase"})
_UI_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class ProvenanceError(ValueError):
    """App provenance is unreadable or violates the platform contract."""


@dataclass(frozen=True)
class UiArtifactReference:
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if type(self.version) is not str or type(self.sha256) is not str:
            raise ProvenanceError("ui reference must use canonical immutable types")


@dataclass(frozen=True)
class AppProvenance:
    schema_version: int
    template_version: int
    platform_contract_version: int
    ui: UiArtifactReference
    capabilities: tuple[str, ...]
    domain_palette_tokens: tuple[str, ...]
    managed_files: tuple[Path, ...]

    def __post_init__(self) -> None:
        if type(self.ui) is not UiArtifactReference:
            raise ProvenanceError("ui must be a UiArtifactReference")
        if type(self.capabilities) is not tuple or any(
            type(item) is not str for item in self.capabilities
        ):
            raise ProvenanceError("capabilities must be an immutable string tuple")
        if type(self.domain_palette_tokens) is not tuple or any(
            type(item) is not str for item in self.domain_palette_tokens
        ):
            raise ProvenanceError(
                "domain_palette_tokens must be an immutable string tuple"
            )
        if type(self.managed_files) is not tuple or any(
            not isinstance(item, Path) for item in self.managed_files
        ):
            raise ProvenanceError("managed_files must be an immutable Path tuple")


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{context} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: frozenset[str], context: str
) -> None:
    if value.keys() != expected:
        raise ProvenanceError(f"{context} must contain exactly the supported keys")


def _positive_integer(value: Any, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProvenanceError(f"{context} must be a positive integer")
    return value


def _sorted_unique_strings(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise ProvenanceError(f"{context} must be a list of strings")
    if value != sorted(value) or len(value) != len(set(value)):
        raise ProvenanceError(f"{context} must be unique and lexically sorted")
    return tuple(value)


def _managed_paths(value: Any) -> tuple[Path, ...]:
    raw_paths = _sorted_unique_strings(value, "managedFiles")
    paths: list[Path] = []
    for raw_path in raw_paths:
        pure_path = PurePosixPath(raw_path)
        if (
            not raw_path
            or "\x00" in raw_path
            or "\\" in raw_path
            or pure_path.is_absolute()
            or pure_path.as_posix() != raw_path
            or raw_path == "."
            or ".." in pure_path.parts
        ):
            raise ProvenanceError("managedFiles must contain canonical relative paths")
        paths.append(Path(*pure_path.parts))
    return tuple(paths)


def _parse_payload(value: Any) -> AppProvenance:
    payload = _object(value, "app provenance")
    _require_exact_keys(payload, _ROOT_KEYS, "app provenance")

    schema_version = _positive_integer(payload["schemaVersion"], "schemaVersion")
    if schema_version != SUPPORTED_PROVENANCE_SCHEMA:
        raise ProvenanceError("schemaVersion is unsupported")
    template_version = _positive_integer(payload["templateVersion"], "templateVersion")
    contract_version = _positive_integer(
        payload["platformContractVersion"], "platformContractVersion"
    )
    if contract_version != SUPPORTED_PLATFORM_CONTRACT:
        raise ProvenanceError("platformContractVersion is unsupported")

    ui = _object(payload["ui"], "ui")
    _require_exact_keys(ui, _UI_KEYS, "ui")
    ui_version = ui["version"]
    if type(ui_version) is not str or not _UI_VERSION.fullmatch(ui_version):
        raise ProvenanceError("ui.version must match x.y.z")
    digest = ui["sha256"]
    if type(digest) is not str or not _SHA256.fullmatch(digest):
        raise ProvenanceError("ui.sha256 must be 64 lowercase hexadecimal characters")

    capabilities = _sorted_unique_strings(payload["capabilities"], "capabilities")
    if any(item not in _SUPPORTED_CAPABILITIES for item in capabilities):
        raise ProvenanceError("capabilities contains an unsupported capability")
    domain_tokens = _sorted_unique_strings(
        payload["domainPaletteTokens"], "domainPaletteTokens"
    )
    if any(not _DOMAIN_TOKEN.fullmatch(item) for item in domain_tokens):
        raise ProvenanceError("domainPaletteTokens contains an invalid token")

    return AppProvenance(
        schema_version=schema_version,
        template_version=template_version,
        platform_contract_version=contract_version,
        ui=UiArtifactReference(version=ui_version, sha256=digest),
        capabilities=capabilities,
        domain_palette_tokens=domain_tokens,
        managed_files=_managed_paths(payload["managedFiles"]),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError("app provenance contains a duplicate key")
        result[key] = value
    return result


def load_provenance(path: Path) -> AppProvenance:
    """Load strict provenance without exposing source paths or parser details."""

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ProvenanceError("cannot read app provenance") from error
    return parse_provenance(content)


def parse_provenance(content: str) -> AppProvenance:
    """Parse provenance bytes already obtained through a trusted read boundary."""

    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, ProvenanceError) as error:
        raise ProvenanceError("cannot read app provenance") from error
    return _parse_payload(payload)


def _payload(provenance: AppProvenance) -> dict[str, Any]:
    return {
        "schemaVersion": provenance.schema_version,
        "templateVersion": provenance.template_version,
        "platformContractVersion": provenance.platform_contract_version,
        "ui": {
            "version": provenance.ui.version,
            "sha256": provenance.ui.sha256,
        },
        "capabilities": list(provenance.capabilities),
        "domainPaletteTokens": list(provenance.domain_palette_tokens),
        "managedFiles": [path.as_posix() for path in provenance.managed_files],
    }


def render_provenance(provenance: AppProvenance) -> bytes:
    """Render validated canonical UTF-8 JSON with a final newline."""

    payload = _payload(provenance)
    _parse_payload(payload)
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def validate_compatibility(
    provenance: AppProvenance, platform: PlatformSpec | None
) -> tuple[str, ...]:
    """Return stable codes for disagreements with a manifest platform contract."""

    if platform is None:
        return ("platform.manifest-missing",)
    diagnostics: list[str] = []
    if platform.contract_version != provenance.platform_contract_version:
        diagnostics.append("platform.contract-version-mismatch")
    if platform.template_version != provenance.template_version:
        diagnostics.append("platform.template-version-mismatch")
    if platform.ui_version != provenance.ui.version:
        diagnostics.append("ui.version-mismatch")
    if platform.capabilities != provenance.capabilities:
        diagnostics.append("platform.capabilities-mismatch")
    return tuple(diagnostics)
