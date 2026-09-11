#!/usr/bin/env python3
"""Create the vendorable shared-UI archive without exposing build internals."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from local_web_server.ui_package import UiPackageError, build_ui_package


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: build_ui_package.py OUTPUT", file=sys.stderr)
        return 2
    try:
        artifact = build_ui_package(REPOSITORY, Path(arguments[0]))
    except UiPackageError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"UI package built: version={artifact.version} sha256={artifact.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
