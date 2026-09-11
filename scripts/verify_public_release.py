#!/usr/bin/env python3
"""Run Local Web's bounded public-release verification gate."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from local_web_server.public_release import PublicReleaseError, verify_public_release


_HELP = """usage: verify_public_release.py [options]

options:
  --repository PATH
  --private-policy PATH
  --history-branch NAME --deny-commit-ids PATH
  --help
"""


@dataclass(frozen=True, slots=True)
class _Arguments:
    repository: Path
    private_policy: Path | None
    history_branch: str | None
    deny_commit_ids: Path | None


def _parse_arguments(argv: list[str]) -> _Arguments | None:
    values: dict[str, str] = {}
    names = {
        "--repository": "repository",
        "--private-policy": "private_policy",
        "--history-branch": "history_branch",
        "--deny-commit-ids": "deny_commit_ids",
    }
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--help" and len(argv) == 1:
            return None
        key = names.get(argument)
        if key is None or key in values or index + 1 >= len(argv):
            raise ValueError
        value = argv[index + 1]
        if not value or value.startswith("--"):
            raise ValueError
        values[key] = value
        index += 2
    history_branch = values.get("history_branch")
    deny_commit_ids = values.get("deny_commit_ids")
    if (history_branch is None) != (deny_commit_ids is None):
        raise ValueError
    return _Arguments(
        repository=Path(values.get("repository", ".")),
        private_policy=(
            None
            if values.get("private_policy") is None
            else Path(values["private_policy"])
        ),
        history_branch=history_branch,
        deny_commit_ids=(
            None
            if deny_commit_ids is None
            else Path(deny_commit_ids)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parse_arguments(list(sys.argv[1:] if argv is None else argv))
    except (TypeError, ValueError):
        print("public-release: invalid arguments", file=sys.stderr)
        return 2
    if arguments is None:
        print(_HELP, end="")
        return 0
    try:
        verify_public_release(
            arguments.repository,
            private_policy_path=arguments.private_policy,
            expected_branch=arguments.history_branch,
            denied_commit_ids_path=arguments.deny_commit_ids,
        )
    except PublicReleaseError as error:
        print(f"public-release verification failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("public-release verification failed: internal error", file=sys.stderr)
        return 1
    print("public-release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
