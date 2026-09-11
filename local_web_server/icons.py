"""Generated Tabler icon catalogue used by manifests and the System Index."""

import json
from pathlib import Path
from types import MappingProxyType


_CATALOGUE_PATH = Path(__file__).resolve().parents[1] / "platform_assets" / "icons.json"
_CATALOGUE = json.loads(_CATALOGUE_PATH.read_text(encoding="utf-8"))

MANIFEST_ICON_NAMES = tuple(_CATALOGUE["manifest"])
ACTION_ICON_NAMES = tuple(_CATALOGUE["actions"])
INTERNAL_ICON_NAMES = tuple(_CATALOGUE["internal"])
ICON_PATHS = MappingProxyType(
    {name: definition["svg"] for name, definition in _CATALOGUE["icons"].items()}
)
MANIFEST_ICON_ALIASES = MappingProxyType(
    {
        "app": "apps",
        "shirt": "shirt-sport",
        "chart": "chart-line",
    }
)


def canonical_manifest_icon(name: str) -> str:
    """Return an allowlisted manifest icon name, resolving legacy aliases."""
    canonical = MANIFEST_ICON_ALIASES.get(name, name)
    if canonical not in MANIFEST_ICON_NAMES:
        raise ValueError("unknown icon")
    return canonical
