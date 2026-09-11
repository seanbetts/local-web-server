"""Strict, read-only discovery of intentional application identities."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .colour import derive_accessible_accent


IDENTITY_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[1] / "platform_assets" / "app-identities.json"
)
LIGHT_SURFACE = "#FFFFFF"
DARK_SURFACE = "#19202B"
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_ACCENT = re.compile(r"^#[0-9A-F]{6}$")


class IdentityCatalogueError(ValueError):
    """Identity metadata is unavailable or invalid."""


@dataclass(frozen=True)
class ManifestIconIdentity:
    name: str
    source: str
    label: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class AccentIdentity:
    name: str
    seed: str
    light: str
    dark: str


@dataclass(frozen=True)
class IdentityAssignment:
    app_id: str
    title: str
    icon: str
    accent: str


@dataclass(frozen=True)
class IdentityReport:
    icons: tuple[ManifestIconIdentity, ...]
    accents: tuple[AccentIdentity, ...]
    assignments: tuple[IdentityAssignment, ...]
    reserved_icons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "icons": [
                {
                    "name": icon.name,
                    "source": icon.source,
                    "label": icon.label,
                    "keywords": list(icon.keywords),
                }
                for icon in self.icons
            ],
            "accents": [asdict(accent) for accent in self.accents],
            "assignments": [
                {
                    "appId": assignment.app_id,
                    "title": assignment.title,
                    "icon": assignment.icon,
                    "accent": assignment.accent,
                }
                for assignment in self.assignments
            ],
            "reservedIcons": list(self.reserved_icons),
        }


@dataclass(frozen=True)
class AppIdentityCatalogue:
    icons: tuple[ManifestIconIdentity, ...]
    accents: tuple[AccentIdentity, ...]
    reserved_icons: tuple[str, ...]

    @classmethod
    def load(cls, path: Path = IDENTITY_CATALOGUE_PATH) -> "AppIdentityCatalogue":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schemaVersion",
                "reservedIcons",
                "icons",
                "accents",
            }:
                raise ValueError
            if payload["schemaVersion"] != 1:
                raise ValueError
            icons = cls._parse_icons(payload["icons"])
            accents = cls._parse_accents(payload["accents"])
            reserved = payload["reservedIcons"]
            if (
                not isinstance(reserved, list)
                or any(not isinstance(name, str) for name in reserved)
                or len(reserved) != len(set(reserved))
                or not set(reserved).issubset({icon.name for icon in icons})
            ):
                raise ValueError
            return cls(icons, accents, tuple(reserved))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise IdentityCatalogueError("application identity catalogue is invalid") from error

    @staticmethod
    def _parse_icons(value: Any) -> tuple[ManifestIconIdentity, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError
        icons: list[ManifestIconIdentity] = []
        for raw in value:
            if not isinstance(raw, dict) or set(raw) != {
                "name",
                "source",
                "label",
                "keywords",
            }:
                raise ValueError
            name, source, label, keywords = (
                raw["name"],
                raw["source"],
                raw["label"],
                raw["keywords"],
            )
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or not isinstance(source, str)
                or not _NAME.fullmatch(source)
                or not isinstance(label, str)
                or not label.strip()
                or not isinstance(keywords, list)
                or not keywords
                or any(
                    not isinstance(keyword, str)
                    or not keyword
                    or keyword != keyword.lower()
                    or not _NAME.fullmatch(keyword)
                    for keyword in keywords
                )
                or len(keywords) != len(set(keywords))
            ):
                raise ValueError
            icons.append(ManifestIconIdentity(name, source, label, tuple(keywords)))
        if len({icon.name for icon in icons}) != len(icons):
            raise ValueError
        return tuple(icons)

    @staticmethod
    def _parse_accents(value: Any) -> tuple[AccentIdentity, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError
        accents: list[AccentIdentity] = []
        for raw in value:
            if not isinstance(raw, dict) or set(raw) != {"name", "seed"}:
                raise ValueError
            name, seed = raw["name"], raw["seed"]
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or not isinstance(seed, str)
                or not _ACCENT.fullmatch(seed)
            ):
                raise ValueError
            accents.append(
                AccentIdentity(
                    name,
                    seed,
                    derive_accessible_accent(seed, LIGHT_SURFACE),
                    derive_accessible_accent(seed, DARK_SURFACE),
                )
            )
        if (
            len({accent.name for accent in accents}) != len(accents)
            or len({accent.seed for accent in accents}) != len(accents)
        ):
            raise ValueError
        return tuple(accents)

    def discover(
        self,
        search: str | None,
        assignments: tuple[IdentityAssignment, ...],
    ) -> IdentityReport:
        icons = self.icons
        if search is not None:
            terms = tuple(
                dict.fromkeys(
                    term.lower() for term in re.findall(r"[A-Za-z0-9-]+", search)
                )
            )
            icons = tuple(
                icon
                for icon in icons
                if icon.name not in self.reserved_icons
                and any(
                    term in {icon.name, icon.source, *icon.keywords}
                    or term in icon.label.lower().split()
                    for term in terms
                )
            )
        return IdentityReport(icons, self.accents, assignments, self.reserved_icons)

    def is_reserved(self, icon: str) -> bool:
        return icon in self.reserved_icons
