"""Deterministic accessible app-accent derivation shared with the UI package."""

import re


_HEX_COLOUR = re.compile(r"^#[0-9A-F]{6}$")


def _channels(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _HEX_COLOUR.fullmatch(value):
        raise ValueError("invalid colour")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _hex(channels: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(value: str) -> float:
    red, green, blue = _channels(value)
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG relative-luminance contrast ratio for two canonical colours."""
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def derive_accessible_accent(seed: str, surface: str) -> str:
    """Return the nearest black/white interpolation reaching 3:1 contrast."""
    source = _channels(seed)
    _channels(surface)
    candidates: set[tuple[int, int, int]] = set()
    for step in range(256):
        candidates.add(
            tuple(round(channel * (255 - step) / 255) for channel in source)
        )
        candidates.add(
            tuple(
                round(channel + (255 - channel) * step / 255)
                for channel in source
            )
        )

    eligible = []
    for candidate in candidates:
        value = _hex(candidate)
        if contrast_ratio(value, surface) >= 3.0:
            distance = sum(
                (candidate[index] - source[index]) ** 2 for index in range(3)
            )
            eligible.append((distance, value))
    if not eligible:  # Black or white always reaches 3:1; keep the contract explicit.
        raise ValueError("cannot derive accessible accent")
    return min(eligible)[1]
