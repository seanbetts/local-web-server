#!/usr/bin/env python3
"""Explicit setup entry point for the local web server."""

import argparse
import sys
from pathlib import Path


repository = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository))

from local_web_server.config import ConfigError
from local_web_server.host_profile import HostProfileError
from local_web_server.install import InstallError
from local_web_server.platform_installation import install_platform


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install_local_web.py")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prepare-tailscale-port-migration", action="store_true",
        help="prepare the temporary dual-listener handover; does not change Tailscale",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        migration_options = (
            {"prepare_tailscale_port_migration": True} if args.prepare_tailscale_port_migration else {}
        )
        result = install_platform(repository, dry_run=args.dry_run, **migration_options)
    except (ConfigError, HostProfileError, InstallError) as error:
        print(f"install-local-web: {error}", file=sys.stderr)
        return 2

    prefix = "Would write" if result.install.dry_run else "Wrote"
    private_preview = result.install.dry_run and bool(result.install.dry_run_summary)
    if private_preview:
        for message in result.install.dry_run_summary:
            print(message)
        print(f"{prefix}: {len(result.install.writes)} managed files")
    else:
        for path in result.install.writes:
            print(f"{prefix}: {path}")
    for message in result.install.messages:
        print(message)
    skill_prefix = "Would link" if result.agent_skill.dry_run else "Linked"
    skill_target = "local-web-app-development" if private_preview else result.agent_skill.target
    print(f"{skill_prefix} agent skill: {skill_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
