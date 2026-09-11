"""Narrow read-only inspection of an application's selected live release."""

from __future__ import annotations

from pathlib import Path

from .runtime import RuntimeLayout, RuntimeLayoutError, read_release_commit


class LiveReleaseInspectionError(RuntimeError):
    """The live release cannot be inspected without exposing runtime detail."""


class RuntimeLiveReleaseInspector:
    """Read the current immutable release through the managed pointer boundary."""

    def inspect(self, runtime_root: Path, app_id: str) -> str | None:
        try:
            layout = RuntimeLayout(runtime_root, app_id)
            return read_release_commit(layout.current)
        except (OSError, RuntimeError, TypeError, ValueError, RuntimeLayoutError) as error:
            raise LiveReleaseInspectionError(
                "live release inspection failed"
            ) from error
